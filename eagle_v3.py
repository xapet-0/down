import argparse
import importlib.util
import json
import os
import queue
import re
import threading
import time
import tkinter as tk
from dataclasses import dataclass
from datetime import datetime, timezone
from tkinter import filedialog, messagebox, scrolledtext, ttk
from typing import List, Optional

from playwright.sync_api import sync_playwright

pyperclip = None
if importlib.util.find_spec("pyperclip"):
    import pyperclip  # type: ignore[assignment]

winsound = None
if importlib.util.find_spec("winsound"):
    import winsound  # type: ignore[assignment]

# =======================================================
# 🦅 EAGLE V3: INDUSTRIAL-GRADE KEEPER
# =======================================================

DEFAULT_DEBUG_URL = "http://localhost:9222"
DEFAULT_MODEL_URL = "https://gemini.google.com/app"
DEFAULT_HISTORY_FILE = "eagle_history.json"
DEFAULT_BATCH = 30
DEFAULT_POLL_DELAY = 1.0
DEFAULT_MAX_RETRIES = 2

SACRED_PROMPT = """SYSTEM MODE: EXPERT TECHNICAL INSTRUCTOR & TRANSLATOR.
TARGET LANGUAGE: ARABIC (Professional, Academic, RTL-Optimized).

⚠️ OBJECTIVE: Translate the transcript to Arabic using the "English-First Dual-Anchor Protocol".

🛑 TERMINOLOGY RULES (THE GOLDEN STANDARD):
1. RULE 1: ENGLISH IS MASTER (The Foundation):
   - All technical terms, core concepts, methodologies, and specific keywords MUST remain in English inside double quotes.
   - DO NOT Arabize the term completely. Keep the English source.

2. RULE 2: THE EXPLANATION (The Support):
   - Immediately after the English term, provide the Arabic translation inside parentheses.
   - FORMAT: "English Term" (الترجمة العربية).

   ✅ CORRECT EXAMPLES:
   - "Time Management" (إدارة الوقت) is essential. -> تعتبر "Time Management" (إدارة الوقت) جوهرية.
   - Avoid the "Emergency Trap" (فخ الطوارئ). -> تجنب الوقوع في "Emergency Trap" (فخ الطوارئ).
   - Use the "Leitner System" (نظام لايتنر). -> استخدم "Leitner System" (نظام لايتنر).

🛑 VISUAL & FORMAT RULES (CRITICAL):
1. RTL ANCHOR: Every single line MUST start with an Arabic word or letter.
   - Bad: "Task Management" مهمة...
   - Good: إن الـ "Task Management" (إدارة المهام) مهمة...
2. BLOCK INTEGRITY: Translate block-by-block. Do not merge lines. Keep timestamps exactly as they are.
3. OUTPUT: Inside a Markdown code block (```srt). No filler text.

INPUT BLOCKS:
"""


def read_clipboard_text():
    if pyperclip:
        return pyperclip.paste()
    try:
        root = tk.Tk()
        root.withdraw()
        text = root.clipboard_get()
        root.destroy()
        return text
    except Exception:
        return ""


class SoundPlayer:
    @staticmethod
    def play_success():
        threading.Thread(target=SoundPlayer._success_sound, daemon=True).start()

    @staticmethod
    def play_error():
        threading.Thread(target=SoundPlayer._error_sound, daemon=True).start()

    @staticmethod
    def _success_sound():
        if winsound:
            winsound.Beep(880, 120)
            winsound.Beep(1040, 120)
            return
        try:
            root = tk.Tk()
            root.withdraw()
            root.bell()
            root.destroy()
        except Exception:
            return

    @staticmethod
    def _error_sound():
        if winsound:
            winsound.Beep(440, 300)
            winsound.Beep(330, 300)
            return
        try:
            root = tk.Tk()
            root.withdraw()
            root.bell()
            root.bell()
            root.destroy()
        except Exception:
            return


@dataclass
class Config:
    debug_url: str = DEFAULT_DEBUG_URL
    model_url: str = DEFAULT_MODEL_URL
    history_file: str = DEFAULT_HISTORY_FILE
    batch_size: int = DEFAULT_BATCH
    poll_delay: float = DEFAULT_POLL_DELAY
    max_retries: int = DEFAULT_MAX_RETRIES
    min_match_ratio: float = 0.8
    auto_copy: bool = True
    use_gui: bool = True


class EagleLog:
    def __init__(self, write_func):
        self.write_func = write_func

    def info(self, message):
        self.write_func(f"🦅 Eagle: {message}")

    def warn(self, message):
        self.write_func(f"⚠️ Eagle: {message}")

    def error(self, message):
        self.write_func(f"❌ Eagle: {message}")


class HistoryManager:
    def __init__(self, path):
        self.path = path
        self._history = self._load()

    def _load(self):
        if not os.path.exists(self.path):
            return {}
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                return json.load(handle)
        except Exception:
            return {}

    def mark_done(self, file_path):
        self._history[os.path.abspath(file_path)] = {
            "status": "DONE",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        self._save()

    def get_status(self, file_path):
        return self._history.get(os.path.abspath(file_path))

    def _save(self):
        with open(self.path, "w", encoding="utf-8") as handle:
            json.dump(self._history, handle, indent=2, ensure_ascii=False)


class TranscriptParser:
    @staticmethod
    def natural_sort_key(text):
        return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", text)]

    @staticmethod
    def read_and_parse_file(file_path, logger):
        try:
            try:
                with open(file_path, "r", encoding="utf-8") as handle:
                    content = handle.read()
            except UnicodeDecodeError:
                with open(file_path, "r", encoding="latin-1") as handle:
                    content = handle.read()

            if file_path.lower().endswith(".vtt"):
                content = re.sub(r"WEBVTT.*?\n", "", content, flags=re.IGNORECASE)
                content = re.sub(r"(\d{2}:\d{2}:\d{2})\.(\d{3})", r"\1,\2", content)

            regex = (
                r"(\d+)\s*\n?(\d{2}:\d{2}:\d{2}[,.]\d{3}\s*-->\s*"
                r"\d{2}:\d{2}:\d{2}[,.]\d{3}).*?\n([\s\S]*?)(?=\n\n\d|\Z)"
            )
            matches = list(re.finditer(regex, content, re.MULTILINE))

            blocks = []
            if not matches and "-->" in content:
                regex_time = (
                    r"(\d{2}:\d{2}:\d{2}[,.]\d{3}\s*-->\s*"
                    r"\d{2}:\d{2}:\d{2}[,.]\d{3}).*?\n([\s\S]*?)(?=\n\n|\Z)"
                )
                for i, match in enumerate(re.finditer(regex_time, content, re.MULTILINE)):
                    blocks.append(
                        {
                            "id": str(i + 1),
                            "time": match.group(1).replace(".", ","),
                            "text": match.group(2).strip(),
                        }
                    )
            else:
                for match in matches:
                    blocks.append(
                        {
                            "id": match.group(1).strip(),
                            "time": match.group(2).replace(".", ","),
                            "text": match.group(3).strip(),
                        }
                    )
            return blocks
        except Exception as exc:
            logger.error(f"Error parsing {os.path.basename(file_path)}: {exc}")
            return []

    @staticmethod
    def extract_srt_blocks(text):
        clean_text = text.replace("```srt", "").replace("```", "").strip()
        regex_block = (
            r"(\d+)\s*\n(\d{2}:\d{2}:\d{2}[,.]\d{3})\s*-->\s*"
            r"(\d{2}:\d{2}:\d{2}[,.]\d{3})\s*\n(.*?)(?=\n\d+\s*\n\d{2}:\d{2}|$)"
        )
        found = re.findall(regex_block, clean_text, re.DOTALL)
        cleaned = []
        for match in found:
            cleaned.append(f"{match[0]}\n{match[1]} --> {match[2]}\n{match[3].strip()}")
        return cleaned

    @staticmethod
    def get_resume_point(output_path):
        if not os.path.exists(output_path):
            return 0
        try:
            with open(output_path, "r", encoding="utf-8") as handle:
                content = handle.read()
            return len(re.findall(r"\d{2}:\d{2}:\d{2}[,.]\d{3}\s*-->", content))
        except Exception:
            return 0

    @staticmethod
    def extract_first_last_ids(text):
        matches = TranscriptParser.extract_srt_blocks(text)
        if not matches:
            return None, None
        first_id = matches[0].splitlines()[0].strip()
        last_id = matches[-1].splitlines()[0].strip()
        return first_id, last_id

    @staticmethod
    def extract_first_last_times(text):
        clean_text = text.replace("```srt", "").replace("```", "").strip()
        regex_time = r"(\\d{2}:\\d{2}:\\d{2}[,.]\\d{3})\\s*-->\\s*(\\d{2}:\\d{2}:\\d{2}[,.]\\d{3})"
        matches = re.findall(regex_time, clean_text)
        if not matches:
            return None, None
        first_time = matches[0][0].replace(".", ",")
        last_time = matches[-1][0].replace(".", ",")
        return first_time, last_time


class GeminiBot:
    def __init__(self, config, logger):
        self.config = config
        self.logger = logger
        self._last_sent = ""

    def connect(self):
        self.logger.info("Connecting to Gemini...")
        playwright = sync_playwright().start()
        browser = playwright.chromium.connect_over_cdp(self.config.debug_url)
        gemini_page = None
        for ctx in browser.contexts:
            for page in ctx.pages:
                if "gemini.google.com" in page.url:
                    gemini_page = page
                    gemini_page.bring_to_front()
                    break
            if gemini_page:
                break

        if not gemini_page:
            gemini_page = browser.contexts[0].new_page()
            gemini_page.goto(self.config.model_url)
            self.logger.warn("Opened new tab. Please login if needed.")
        return playwright, browser, gemini_page

    def _find_input(self, page):
        candidates = [
            "div[contenteditable='true']",
            "textarea",
            "div[role='textbox']",
            "textarea[aria-label*='Message']",
        ]
        for selector in candidates:
            locator = page.locator(selector)
            if locator.count() > 0 and locator.first.is_visible():
                return locator.first
        return None

    def _collect_response_text(self, page):
        script = """() => {
            const candidates = Array.from(document.querySelectorAll('model-response, div[role="article"], div[data-response-id]'));
            if (!candidates.length) {
                const alt = Array.from(document.querySelectorAll('main div'));
                if (!alt.length) return '';
                const last = alt[alt.length - 1];
                return last.innerText || '';
            }
            const last = candidates[candidates.length - 1];
            return last.innerText || '';
        }"""
        try:
            return page.evaluate(script)
        except Exception:
            return ""

    def _is_streaming(self, page):
        script = """() => {
            const resp = document.querySelectorAll('model-response');
            if (!resp.length) return false;
            const last = resp[resp.length - 1];
            if (last.querySelector('.streaming')) return true;
            if (last.innerHTML.includes('cursor-blink')) return true;
            return false;
        }"""
        try:
            return bool(page.evaluate(script))
        except Exception:
            return False

    def send_batch(self, page, blocks):
        text_payload = "".join(
            f"{b['id']}\n{b['time']}\n{b['text']}\n\n" for b in blocks
        )
        msg = SACRED_PROMPT + "\n" + text_payload.strip()

        self.logger.info(
            f"Sending batch [{blocks[0]['id']} -> {blocks[-1]['id']}] ({len(blocks)} lines)."
        )

        if self._last_sent == msg:
            self.logger.warn("Duplicate payload detected. Skipping send.")
            return False

        textarea = self._find_input(page)
        if not textarea:
            self.logger.error("Input box not found.")
            return False

        try:
            textarea.click(force=True)
            time.sleep(0.2)
            page.keyboard.press("Control+A")
            page.keyboard.press("Backspace")
            if pyperclip:
                pyperclip.copy(msg)
                page.keyboard.press("Control+V")
            else:
                textarea.type(msg, delay=2)
            time.sleep(0.5)
            page.keyboard.press("Enter")
            self._last_sent = msg
            return True
        except Exception as exc:
            self.logger.error(f"Failed to send batch: {exc}")
            return False

    def wait_for_completion(self, page):
        self.logger.info("Waiting for Gemini to finish...")
        last_len = 0
        stability = 0
        last_change = time.time()

        while True:
            streaming = self._is_streaming(page)
            current_text = self._collect_response_text(page)
            current_len = len(current_text)

            if streaming:
                stability = 0
                last_change = time.time()
                time.sleep(self.config.poll_delay)
                continue

            if current_len > 50:
                if current_len == last_len:
                    stability += 1
                else:
                    stability = 0
                    last_len = current_len
                    last_change = time.time()

                if stability >= 2:
                    self.logger.info("Gemini finished.")
                    return True

            if time.time() - last_change > 45:
                self.logger.warn("Timeout waiting for Gemini output.")
                return False

            time.sleep(self.config.poll_delay)

    def fetch_latest_response(self, page):
        text = self._collect_response_text(page)
        if text.strip():
            return text
        self.logger.warn("DOM extraction failed. Trying clipboard fallback...")
        return self._copy_response_via_keyboard(page)

    def _copy_response_via_keyboard(self, page):
        try:
            page.keyboard.press("Control+F")
            page.keyboard.type("```srt")
            page.keyboard.press("Enter")
            time.sleep(0.2)
            page.keyboard.press("Escape")
            page.keyboard.press("Control+A")
            page.keyboard.press("Control+C")
            time.sleep(0.2)
        except Exception:
            return ""
        return read_clipboard_text()


class ManualDialog(tk.Toplevel):
    def __init__(
        self,
        parent,
        prompt,
        expected_first,
        expected_last,
        expected_first_time,
        expected_last_time,
        last_saved_id,
    ):
        super().__init__(parent)
        self.title("Manual Handover Required")
        self.geometry("650x360")
        self.result_text = ""
        self.expected_first = expected_first
        self.expected_last = expected_last
        self.last_saved_id = last_saved_id
        self.expected_first_time = expected_first_time
        self.expected_last_time = expected_last_time
        self.attributes("-topmost", True)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        ttk.Label(self, text=prompt, wraplength=600).pack(pady=8)
        ttk.Label(
            self,
            text=(
                f"Current Progress: IDs {expected_first} -> {expected_last} | "
                f"Times {expected_first_time} -> {expected_last_time}"
            ),
        ).pack(pady=2)
        ttk.Label(self, text=f"Last Successfully Saved: ID {last_saved_id}").pack(pady=2)
        self.text_area = scrolledtext.ScrolledText(self, height=8, wrap="word")
        self.text_area.pack(fill="both", expand=True, padx=10, pady=6)
        ttk.Button(self, text="Confirm", command=self._on_confirm).pack(pady=8)

    def _on_confirm(self):
        pasted = self.text_area.get("1.0", "end").strip()
        first_id, last_id = TranscriptParser.extract_first_last_ids(pasted)
        first_time, last_time = TranscriptParser.extract_first_last_times(pasted)
        if (first_id and last_id) or (first_time and last_time):
            if first_id and last_id:
                id_match = first_id == self.expected_first and last_id == self.expected_last
            else:
                id_match = False
            if first_time and last_time:
                time_match = (
                    first_time == self.expected_first_time
                    and last_time == self.expected_last_time
                )
            else:
                time_match = False
            if id_match or time_match:
                self.result_text = pasted
                self.destroy()
                return
            SoundPlayer.play_error()
            messagebox.showwarning(
                "Mismatch",
                f"Mismatch! Expected IDs {self.expected_first}-{self.expected_last} or "
                f"times {self.expected_first_time}-{self.expected_last_time}. Please copy the correct part.",
            )
            return
        SoundPlayer.play_error()
        messagebox.showwarning(
            "Mismatch",
            "Could not detect block IDs or timestamps. Please paste the correct SRT chunk.",
        )

    def _on_close(self):
        self.destroy()


class EagleProcessor:
    def __init__(self, config, logger, manual_provider):
        self.config = config
        self.logger = logger
        self.history = HistoryManager(config.history_file)
        self.manual_provider = manual_provider
        self.last_saved_id = "N/A"

    def get_all_files(self, root_path):
        all_files = []
        for root, dirs, files in os.walk(root_path):
            dirs.sort(key=TranscriptParser.natural_sort_key)
            files.sort(key=TranscriptParser.natural_sort_key)
            for filename in files:
                if filename.endswith((".srt", ".vtt")) and "_AR" not in filename:
                    all_files.append(os.path.join(root, filename))
        return all_files

    def build_status(self, file_path):
        out_path = file_path.replace(".srt", "_AR.srt").replace(".vtt", "_AR.srt")
        history = self.history.get_status(file_path)
        if history:
            return "✅ DONE"
        if os.path.exists(out_path):
            done_blocks = TranscriptParser.get_resume_point(out_path)
            if done_blocks > 0:
                return f"🔄 RESUME ({done_blocks} blocks)"
        return "⬜ TODO"

    def process(self, file_paths, bot, page):
        for file_path in file_paths:
            self.process_file(file_path, bot, page)

    def process_file(self, file_path, bot, page):
        self.logger.info(f"Processing {os.path.basename(file_path)}")
        total_blocks = TranscriptParser.read_and_parse_file(file_path, self.logger)
        if not total_blocks:
            return

        out_path = file_path.replace(".srt", "_AR.srt").replace(".vtt", "_AR.srt")
        start_index = TranscriptParser.get_resume_point(out_path)
        if start_index > 0:
            self.logger.info(f"Resuming from block {start_index}.")
            if start_index <= len(total_blocks):
                self.last_saved_id = total_blocks[start_index - 1]["id"]

        if start_index >= len(total_blocks):
            self.logger.info("File already finished.")
            self.history.mark_done(file_path)
            SoundPlayer.play_success()
            return

        i = start_index
        while i < len(total_blocks):
            chunk = total_blocks[i : i + self.config.batch_size]
            response_text = self._run_with_retries(bot, page, chunk)
            if not response_text:
                self.logger.warn("Automation failed twice. Waiting for manual input...")
                SoundPlayer.play_error()
                response_text = self.manual_provider(
                    "Automation Failed. Please manually copy the translation from the browser and paste it here.",
                    chunk[0]["id"],
                    chunk[-1]["id"],
                    chunk[0]["time"],
                    chunk[-1]["time"],
                    self.last_saved_id,
                )

            if not response_text:
                self.logger.error("No translation text provided. Aborting file.")
                return

            srt_matches = TranscriptParser.extract_srt_blocks(response_text)
            required = max(1, int(len(chunk) * self.config.min_match_ratio))
            if len(srt_matches) >= required:
                self._log_chunk_details("Saving", chunk, srt_matches)
                with open(out_path, "a", encoding="utf-8-sig") as handle:
                    if i > 0:
                        handle.write("\n\n")
                    handle.write("\n\n".join(srt_matches))
                i += len(chunk)
                self.last_saved_id = chunk[-1]["id"]
                self.logger.info("Batch saved.")
            else:
                self.logger.warn("Mismatch detected. Switching to manual confirmation.")
                SoundPlayer.play_error()
                response_text = self.manual_provider(
                    "Mismatch detected. Paste correct translation here.",
                    chunk[0]["id"],
                    chunk[-1]["id"],
                    chunk[0]["time"],
                    chunk[-1]["time"],
                    self.last_saved_id,
                )
                srt_matches = TranscriptParser.extract_srt_blocks(response_text)
                if len(srt_matches) >= required:
                    self._log_chunk_details("Manual save", chunk, srt_matches)
                    with open(out_path, "a", encoding="utf-8-sig") as handle:
                        if i > 0:
                            handle.write("\n\n")
                        handle.write("\n\n".join(srt_matches))
                    i += len(chunk)
                    self.last_saved_id = chunk[-1]["id"]
                else:
                    self.logger.error("Manual paste still invalid. Aborting file.")
                    return

        self.history.mark_done(file_path)
        self.logger.info(f"Completed {os.path.basename(out_path)}")
        SoundPlayer.play_success()

    def _run_with_retries(self, bot, page, chunk):
        for attempt in range(1, self.config.max_retries + 1):
            self.logger.info(f"Sending batch attempt {attempt}...")
            if not bot.send_batch(page, chunk):
                self.logger.warn("Send failed.")
                continue
            finished = bot.wait_for_completion(page)
            if not finished:
                self.logger.warn("Gemini did not finish in time.")
                continue
            response_text = ""
            if self.config.auto_copy:
                response_text = bot.fetch_latest_response(page)
            if response_text.strip():
                return response_text
            self.logger.warn("Extraction failed.")
        return ""

    def _log_chunk_details(self, label, chunk, matches):
        src_first = chunk[0]["id"]
        src_last = chunk[-1]["id"]
        out_first = matches[0].splitlines()[0] if matches else "?"
        out_last = matches[-1].splitlines()[0] if matches else "?"
        self.logger.info(
            f"{label}: src {src_first}-{src_last}, out {out_first}-{out_last}"
        )


class EagleGUI:
    def __init__(self, config):
        self.config = config
        self.root = tk.Tk()
        self.root.title("🦅 Eagle V3 - Industrial Subtitle Keeper")
        self.root.geometry("1020x760")
        self.files: List[str] = []
        self.selected_dir = tk.StringVar()
        self.auto_copy_var = tk.BooleanVar(value=config.auto_copy)
        self.batch_var = tk.IntVar(value=config.batch_size)
        self.status_queue = queue.Queue()
        self.worker_thread: Optional[threading.Thread] = None
        self._build_ui()

    def _build_ui(self):
        header = ttk.Frame(self.root, padding=10)
        header.pack(fill="x")

        ttk.Label(header, text="🦅 Eagle V3", font=("Segoe UI", 16, "bold")).pack(
            side="left"
        )
        ttk.Label(
            header, text="Industrial Gemini Subtitle Automation", font=("Segoe UI", 10)
        ).pack(side="left", padx=10)

        controls = ttk.Frame(self.root, padding=10)
        controls.pack(fill="x")

        ttk.Button(controls, text="📂 Choose Folder", command=self._choose_folder).pack(
            side="left"
        )
        ttk.Label(controls, textvariable=self.selected_dir).pack(
            side="left", padx=8
        )
        ttk.Label(controls, text="Batch Size").pack(side="left", padx=(20, 4))
        ttk.Spinbox(controls, from_=5, to=120, textvariable=self.batch_var, width=6).pack(
            side="left"
        )
        ttk.Checkbutton(controls, text="Auto-copy", variable=self.auto_copy_var).pack(
            side="left", padx=10
        )
        ttk.Button(controls, text="🚀 Start", command=self._start).pack(side="left")
        ttk.Button(controls, text="🛑 Stop", command=self._stop).pack(side="left", padx=5)

        list_frame = ttk.LabelFrame(self.root, text="Files", padding=10)
        list_frame.pack(fill="both", expand=True, padx=10, pady=5)

        self.tree = ttk.Treeview(list_frame, columns=("status", "path"), show="headings")
        self.tree.heading("status", text="Status")
        self.tree.heading("path", text="File")
        self.tree.column("status", width=170)
        self.tree.column("path", width=720)
        self.tree.pack(fill="both", expand=True)

        manual_frame = ttk.LabelFrame(self.root, text="Manual Paste Area", padding=10)
        manual_frame.pack(fill="both", expand=True, padx=10, pady=5)
        self.manual_text = scrolledtext.ScrolledText(manual_frame, height=6, wrap="word")
        self.manual_text.pack(fill="both", expand=True)

        log_frame = ttk.LabelFrame(self.root, text="Log", padding=10)
        log_frame.pack(fill="both", expand=True, padx=10, pady=5)
        self.log_box = tk.Text(log_frame, height=8, state="disabled", wrap="word")
        self.log_box.pack(fill="both", expand=True)

    def _choose_folder(self):
        folder = filedialog.askdirectory()
        if folder:
            self.selected_dir.set(folder)
            self._scan_files()

    def _scan_files(self):
        self.tree.delete(*self.tree.get_children())
        processor = EagleProcessor(self.config, EagleLog(self._log), self._manual_dialog)
        self.files = processor.get_all_files(self.selected_dir.get())
        for file_path in self.files:
            status = processor.build_status(file_path)
            rel_path = os.path.relpath(file_path, self.selected_dir.get())
            self.tree.insert("", "end", values=(status, rel_path))
        self._log(f"Found {len(self.files)} files.")

    def _start(self):
        if not self.selected_dir.get():
            messagebox.showwarning("Select folder", "Please choose a folder first.")
            return
        if self.worker_thread and self.worker_thread.is_alive():
            messagebox.showwarning("Running", "Process already running.")
            return
        self.config.batch_size = self.batch_var.get()
        self.config.auto_copy = self.auto_copy_var.get()
        self.worker_thread = threading.Thread(target=self._run_processing, daemon=True)
        self.worker_thread.start()
        self._log("Launch sequence initiated.")
        self.root.after(200, self._poll_status)

    def _stop(self):
        messagebox.showinfo("Stop", "Close the app to stop the process safely.")

    def _run_processing(self):
        logger = EagleLog(self.status_queue.put)
        processor = EagleProcessor(self.config, logger, self._manual_dialog)
        bot = GeminiBot(self.config, logger)
        playwright, browser, page = bot.connect()
        try:
            processor.process(self.files, bot, page)
        finally:
            browser.close()
            playwright.stop()
            logger.info("Session closed.")

    def _poll_status(self):
        while not self.status_queue.empty():
            message = self.status_queue.get()
            self._log(message)
        if self.worker_thread and self.worker_thread.is_alive():
            self.root.after(200, self._poll_status)

    def _manual_dialog(
        self,
        prompt,
        expected_first,
        expected_last,
        expected_first_time,
        expected_last_time,
        last_saved_id,
    ):
        manual_text = self.manual_text.get("1.0", "end").strip()
        if manual_text:
            first_id, last_id = TranscriptParser.extract_first_last_ids(manual_text)
            first_time, last_time = TranscriptParser.extract_first_last_times(manual_text)
            id_match = (
                first_id == expected_first and last_id == expected_last
                if first_id and last_id
                else False
            )
            time_match = (
                first_time == expected_first_time and last_time == expected_last_time
                if first_time and last_time
                else False
            )
            if id_match or time_match:
                self.manual_text.delete("1.0", "end")
                return manual_text
            SoundPlayer.play_error()
            messagebox.showwarning(
                "Mismatch",
                f"Mismatch! Expected IDs {expected_first}-{expected_last} or "
                f"times {expected_first_time}-{expected_last_time}. Please copy the correct part.",
            )
            return ""
        dialog = ManualDialog(
            self.root,
            prompt,
            expected_first,
            expected_last,
            expected_first_time,
            expected_last_time,
            last_saved_id,
        )
        self.root.wait_window(dialog)
        return dialog.result_text

    def _log(self, message):
        self.log_box.configure(state="normal")
        self.log_box.insert("end", message + "\n")
        self.log_box.configure(state="disabled")
        self.log_box.see("end")

    def run(self):
        self.root.mainloop()


def run_cli(config):
    logger = EagleLog(print)

    def manual_provider(
        prompt,
        expected_first,
        expected_last,
        expected_first_time,
        expected_last_time,
        last_saved_id,
    ):
        print(prompt)
        print(
            f"Expected IDs: {expected_first} -> {expected_last} | "
            f"Times: {expected_first_time} -> {expected_last_time}"
        )
        print(f"Last Saved ID: {last_saved_id}")
        pasted = input("Paste translation: ")
        first_id, last_id = TranscriptParser.extract_first_last_ids(pasted)
        first_time, last_time = TranscriptParser.extract_first_last_times(pasted)
        id_match = (
            first_id == expected_first and last_id == expected_last
            if first_id and last_id
            else False
        )
        time_match = (
            first_time == expected_first_time and last_time == expected_last_time
            if first_time and last_time
            else False
        )
        if not (id_match or time_match):
            SoundPlayer.play_error()
            print(
                f"Mismatch! Expected IDs {expected_first}-{expected_last} or "
                f"times {expected_first_time}-{expected_last_time}. Try again."
            )
            return ""
        return pasted

    processor = EagleProcessor(config, logger, manual_provider)
    target_dir = input("📂 Root Directory: ").strip().replace('"', "")
    if not os.path.exists(target_dir):
        logger.error("Directory not found.")
        return
    files = processor.get_all_files(target_dir)
    if not files:
        logger.warn("No files found.")
        return
    logger.info(f"Found {len(files)} files.")
    for file_path in files:
        status = processor.build_status(file_path)
        logger.info(f"{status} {os.path.relpath(file_path, target_dir)}")
    logger.info("Starting processing...")
    bot = GeminiBot(config, logger)
    playwright, browser, page = bot.connect()
    try:
        processor.process(files, bot, page)
    finally:
        browser.close()
        playwright.stop()
        logger.info("Session closed.")


def main():
    parser = argparse.ArgumentParser(description="Eagle V3 - Subtitle Translator")
    parser.add_argument("--cli", action="store_true", help="Run in CLI mode")
    parser.add_argument("--debug-url", default=DEFAULT_DEBUG_URL)
    parser.add_argument("--model-url", default=DEFAULT_MODEL_URL)
    parser.add_argument("--history-file", default=DEFAULT_HISTORY_FILE)
    parser.add_argument("--batch", type=int, default=DEFAULT_BATCH)
    parser.add_argument("--no-auto-copy", action="store_true")
    args = parser.parse_args()

    config = Config(
        debug_url=args.debug_url,
        model_url=args.model_url,
        history_file=args.history_file,
        batch_size=args.batch,
        auto_copy=not args.no_auto_copy,
        use_gui=not args.cli,
    )

    if config.use_gui:
        try:
            app = EagleGUI(config)
            app.run()
        except tk.TclError:
            run_cli(config)
    else:
        run_cli(config)


if __name__ == "__main__":
    main()
