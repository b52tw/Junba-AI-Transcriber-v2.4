from __future__ import annotations
import json
import hashlib
import threading
import time
from pathlib import Path
from PySide6.QtCore import QThread, Signal
from app.core.audio_tools import ensure_split_audio, audio_duration_seconds, split_output_dir
from app.core.models import TranscriptResult, Segment
from app.providers.local_whisper import LocalWhisperProvider
from app.providers.gemini import GeminiProvider
from app.exporters.exporters import export_all
from app.core.text_normalize import normalize_result_traditional, to_traditional_taiwan


class TranscribeWorker(QThread):
    status = Signal(str)
    progress = Signal(int)
    stage_progress = Signal(int)
    stage_text = Signal(str)
    log = Signal(str)
    checkpoint = Signal(str)
    file_done = Signal(str)
    failed = Signal(str)
    finished_ok = Signal()
    cancelled = Signal()

    def __init__(self, files, output_dir, mode, split_minutes, model_name, language,
                 api_key, diarization, timestamps, smart, traditional_output, formats):
        super().__init__()
        self.files = files
        self.output_dir = Path(output_dir)
        self.mode = mode
        self.split_minutes = split_minutes
        self.model_name = model_name
        self.language = language
        self.api_key = api_key
        self.diarization = diarization
        self.timestamps = timestamps
        self.smart = smart
        self.traditional_output = traditional_output
        self.formats = formats
        self._pause = threading.Event()
        self._stop = threading.Event()
        self._pause.set()

    def pause(self):
        self._pause.clear()

    def resume(self):
        self._pause.set()

    def stop(self):
        self._stop.set()
        self._pause.set()

    def _wait(self):
        while not self._pause.is_set() and not self._stop.is_set():
            time.sleep(0.1)

    def _set_stage(self, text: str, pct: int = -1):
        self.stage_text.emit(text)
        self.stage_progress.emit(pct)
        self.status.emit(text)
        self.log.emit(text)

    def _chunk_progress(self, fi: int, ci: int, chunk_count: int, total_files: int, pct: int, detail: str = ''):
        pct = max(0, min(100, int(pct)))
        self.stage_progress.emit(pct)
        if detail:
            self.stage_text.emit(detail)
        # Reserve first 15% for preprocessing, 80% for recognition, 5% for export.
        within_file = (ci + pct / 100.0) / max(1, chunk_count)
        recognize_fraction = (fi + within_file) / max(1, total_files)
        overall = 15 + int(recognize_fraction * 80)
        self.progress.emit(max(15, min(95, overall)))

    def _effective_split(self, duration: float) -> int:
        effective_split = int(self.split_minutes)
        if self.mode == 'Google Gemini':
            max_minutes = 30 if (self.diarization or self.timestamps) else 60
            if duration > max_minutes * 60 and (effective_split == 0 or effective_split > max_minutes):
                effective_split = max_minutes
                self.log.emit(f'依 Gemini 音訊上限自動切為每 {max_minutes} 分鐘。')
        return effective_split

    def _prepare_plans(self):
        """Split all source files before model/API initialization.

        This makes large-file limits explicit and lets the split files be reused by
        this app or other transcription software from the visible 切割音檔 folder.
        """
        plans = []
        total_files = max(1, len(self.files))
        for fi, source in enumerate(self.files):
            if self._stop.is_set():
                break
            self._wait()
            srcp = Path(source)
            if not srcp.exists():
                raise FileNotFoundError(f'找不到音檔：{source}')
            duration = audio_duration_seconds(source)
            effective_split = self._effective_split(duration)
            if effective_split > 0 and (duration <= 0 or duration > effective_split * 60):
                split_dir = split_output_dir(str(self.output_dir), source, effective_split)
                self._set_stage(f'先切割音檔：{srcp.name}', 0)
                def split_progress(p, _fi=fi):
                    self.stage_progress.emit(p)
                    self.progress.emit(min(14, int(((_fi + p / 100.0) / total_files) * 15)))
                chunks = ensure_split_audio(
                    source, split_dir, effective_split,
                    progress_cb=split_progress,
                    cancel_cb=self._stop.is_set,
                )
                self.log.emit(f'切割完成：{len(chunks)} 段｜{split_dir}')
            else:
                chunks = [source]
                self.progress.emit(min(14, int(((fi + 1) / total_files) * 15)))
                if effective_split > 0:
                    self.log.emit(f'{srcp.name} 未超過 {effective_split} 分鐘，不需實際切割。')
            plans.append({
                'source': source,
                'duration': duration,
                'effective_split': effective_split,
                'chunks': chunks,
            })
        self.progress.emit(15)
        return plans

    def run(self):
        try:
            self.output_dir.mkdir(parents=True, exist_ok=True)
            # v2.4: split FIRST, then initialize Whisper/Gemini.
            self._set_stage('準備音檔；需要切割的檔案會先完成切割', 0)
            plans = self._prepare_plans()
            if self._stop.is_set():
                self.cancelled.emit()
                return

            local = None
            gemini = None
            if self.mode in ('離線 Whisper', '混合模式'):
                self._set_stage('載入 Whisper 模型中；第一次下載 large-v3 可能需要數分鐘…', -1)
                local = LocalWhisperProvider(self.model_name)
                self._set_stage(f'Whisper 已就緒：{local.device} / {local.compute_type}', 100)
            if self.mode in ('Google Gemini', '混合模式'):
                self._set_stage('初始化 Google Gemini…', -1)
                gemini = GeminiProvider(self.api_key)
                self._set_stage('Google Gemini 已就緒', 100)

            total_files = max(1, len(plans))
            for fi, plan in enumerate(plans):
                if self._stop.is_set():
                    break
                self._wait()
                source = plan['source']
                srcp = Path(source)
                effective_split = plan['effective_split']
                chunks = plan['chunks']

                try:
                    sig = f'{srcp.resolve()}|{srcp.stat().st_size}|{srcp.stat().st_mtime_ns}|{self.mode}|{self.model_name}|{effective_split}|{self.language}|{self.diarization}|{self.timestamps}|{self.smart}|{self.traditional_output}'
                except Exception:
                    sig = f'{source}|{self.mode}|{self.model_name}|{effective_split}|{self.language}|{self.diarization}|{self.timestamps}|{self.smart}|{self.traditional_output}'
                profile = hashlib.sha1(sig.encode('utf-8')).hexdigest()[:12]
                work = self.output_dir / '.junba_cache' / f'{srcp.stem}_{profile}'
                work.mkdir(parents=True, exist_ok=True)

                all_segments = []
                all_text = []
                running_offset = 0.0
                for ci, chunk in enumerate(chunks):
                    if self._stop.is_set():
                        break
                    self._wait()
                    cache_file = work / f'result_{ci:03d}.json'
                    if cache_file.exists():
                        self._set_stage(f'讀取快取：{ci+1}/{len(chunks)}', 100)
                        r = self._load_cached_result(cache_file)
                        if self.traditional_output and self.language in ('zh', 'auto'):
                            r = normalize_result_traditional(r)
                        self._chunk_progress(fi, ci, len(chunks), total_files, 100, '已載入快取')
                    else:
                        self._set_stage(f'辨識 {srcp.name}｜區段 {ci+1}/{len(chunks)}', 0)
                        cb = lambda p, d='', _fi=fi, _ci=ci, _n=len(chunks): self._chunk_progress(_fi, _ci, _n, total_files, p, d)
                        if self.mode == 'Google Gemini':
                            lang_codes = self._gemini_language_codes(self.language)
                            r = gemini.transcribe(chunk, self.diarization, self.timestamps, self.smart,
                                                 language_codes=lang_codes, progress_cb=cb)
                        else:
                            lang = None if self.language == 'auto' else self.language
                            r = local.transcribe(chunk, lang, stop_flag=self._stop.is_set,
                                                 progress_cb=cb, wait_cb=self._wait)
                        if self._stop.is_set():
                            break
                        if self.traditional_output and self.language in ('zh', 'auto'):
                            r = normalize_result_traditional(r)
                            self.log.emit('已轉為繁體中文（台灣用字）。')
                        self._save_cached_result(cache_file, r)
                    # Use actual chunk duration rather than assuming exact segment length.
                    for s in r.segments:
                        all_segments.append(Segment(s.start + running_offset, s.end + running_offset, s.text, s.speaker))
                    all_text.append(r.text)
                    running_offset += max(0.0, audio_duration_seconds(chunk))
                    self._write_checkpoint(source, ci + 1, len(chunks), fi, total_files)

                if self._stop.is_set():
                    break
                final_text = '\n'.join(x for x in all_text if x)
                if self.mode == '混合模式' and gemini:
                    self._set_stage('Gemini 整理逐字稿…', 0)
                    final_text = gemini.postprocess_text(final_text, target='繁體中文（台灣）', progress_cb=lambda p, d='': (self.stage_progress.emit(p), self.stage_text.emit(d)))
                if self.traditional_output and self.language in ('zh', 'auto'):
                    final_text = to_traditional_taiwan(final_text)
                    for seg in all_segments:
                        seg.text = to_traditional_taiwan(seg.text)
                result = TranscriptResult(final_text, all_segments, engine=self.mode)
                self._set_stage('匯出 Word / 字幕檔…', -1)
                base = self.output_dir / f'{srcp.stem}_逐字稿'
                paths = export_all(result, str(base), self.formats)
                self.stage_progress.emit(100)
                self.file_done.emit('\n'.join(paths))
                self.progress.emit(95 + int(((fi + 1) / total_files) * 5))

            if self._stop.is_set():
                self._set_stage('工作已停止；已完成區段仍保留在快取。', 0)
                self.cancelled.emit()
            else:
                self.progress.emit(100)
                self._set_stage('全部工作完成', 100)
                self.finished_ok.emit()
        except Exception as e:
            self.failed.emit(f'{type(e).__name__}: {e}')

    @staticmethod
    def _gemini_language_codes(language: str) -> list[str]:
        return {'en': ['en-US'], 'ja': ['ja-JP']}.get(language, [])

    @staticmethod
    def _save_cached_result(path, result):
        payload = {
            'text': result.text,
            'language': result.language,
            'engine': result.engine,
            'segments': [
                {'start': s.start, 'end': s.end, 'text': s.text, 'speaker': s.speaker}
                for s in result.segments
            ],
        }
        Path(path).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')

    @staticmethod
    def _load_cached_result(path):
        payload = json.loads(Path(path).read_text(encoding='utf-8'))
        segs = [Segment(**x) for x in payload.get('segments', [])]
        return TranscriptResult(payload.get('text', ''), segs, payload.get('language'), payload.get('engine', ''))

    def _write_checkpoint(self, source, completed, total, file_index, total_files):
        p = self.output_dir / '.junba_checkpoint.json'
        data = {
            'source': source,
            'completed_chunks': completed,
            'total_chunks': total,
            'file_index': file_index + 1,
            'total_files': total_files,
            'mode': self.mode,
            'model': self.model_name,
            'updated_at': time.strftime('%Y-%m-%d %H:%M:%S'),
        }
        p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
        self.checkpoint.emit(f'進度已儲存：{completed}/{total} 區段｜{p}')
