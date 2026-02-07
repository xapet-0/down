import argparse
import json
import os
import queue
import re
import threading
import time
import tkinter as tk
from dataclasses import dataclass
from datetime import datetime
from tkinter import filedialog, messagebox, ttk

import pyperclip
from playwright.sync_api import sync_playwright

# =======================================================
# 🦅 EAGLE V110: THE KEEPER (Automated + GUI)
# =======================================================

DEFAULT_DEBUG_URL = "http://localhost:9222"
DEFAULT_MODEL_URL = "https://gemini.google.com/app"
DEFAULT_HISTORY_FILE = "eagle_history.json"
DEFAULT_BATCH = 30
DEFAULT_POLL_DELAY = 1

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


@dataclass
class Config:
    debug_url: str = DEFAULT_DEBUG_URL
    model_url: str = DEFAULT_MODEL_URL
    history_file: str = DEFAULT_HISTORY_FILE
    initial_batch: int = DEFAULT_BATCH
    poll_delay: float = DEFAULT_POLL_DELAY
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
            "timestamp": datetime.utcnow().isoformat(),
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


class GeminiBot:
    def __init__(self, config, logger):
        self.config = config
        self.logger = logger

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

    def send_batch(self, page, blocks):
        self.logger.info("Refreshing Gemini tab...")
        try:
            page.reload()
            page.wait_for_selector("div[contenteditable='true'], textarea", timeout=15000)
            time.sleep(1.5)
        except Exception:
            pass

        text_payload = "".join(
            f"{b['id']}\n{b['time']}\n{b['text']}\n\n" for b in blocks
        )
        msg = SACRED_PROMPT + "\n" + text_payload.strip()

        self.logger.info(
            f"Sending batch [{blocks[0]['id']} -> {blocks[-1]['id']}] ({len(blocks)} lines)."
        )

        textarea = None
        try:
            textbox = page.get_by_role("textbox")
            if textbox.count() > 0:
                for i in range(textbox.count()):
                    if textbox.nth(i).is_visible():
                        textarea = textbox.nth(i)
                        break
            if not textarea:
                textarea = page.locator("div[contenteditable='true'], textarea").first
        except Exception:
            pass

        if not textarea or not textarea.is_visible():
            self.logger.error("Input box not found.")
            return False

        try:
            textarea.click(force=True)
            time.sleep(0.5)
            page.evaluate(
                """(data) => {
                    const el = document.querySelector('div[contenteditable="true"]');
                    if (el) {
                        el.innerText = data.text;
                        el.dispatchEvent(new Event('input', { bubbles: true }));
                        el.scrollIntoView();
                    }
                }""",
                {"text": msg},
            )
            time.sleep(1.0)

            if not textarea.inner_text().startswith("SYSTEM:"):
                pyperclip.copy(msg)
                page.keyboard.press("Control+A")
                page.keyboard.press("Control+V")
                time.sleep(1)

            page.keyboard.press("Enter")
            return True
        except Exception as exc:
            self.logger.error(f"Failed to send batch: {exc}")
            return False

    def wait_for_completion(self, page):
        self.logger.info("Watching Gemini output...")
        time.sleep(3)
        last_len = 0
        stability = 0

        while True:
            status = page.evaluate(
                """() => {
                    const responses = document.querySelectorAll('model-response');
                    if (responses.length === 0) return {streaming: false, len: 0};
                    const last = responses[responses.length - 1];
                    const isStreaming = last.querySelector('.streaming') !== null
                        || last.innerHTML.includes('cursor-blink');
                    return {streaming: isStreaming, len: last.innerText.length};
                }"""
            )

            if status["streaming"]:
                stability = 0
                time.sleep(self.config.poll_delay)
                continue

            if not status["streaming"] and status["len"] > 50:
                if status["len"] == last_len:
                    stability += 1
                else:
                    stability = 0
                    last_len = status["len"]

                if stability >= 2:
                    self.logger.info("Gemini finished.")
                    return True

            time.sleep(self.config.poll_delay)

    def fetch_latest_response(self, page):
        try:
            return page.evaluate(
                """() => {
                    const responses = document.querySelectorAll('model-response');
                    if (responses.length === 0) return '';
                    return responses[responses.length - 1].innerText || '';
                }"""
            )
        except Exception:
            return ""


class EagleProcessor:
    def __init__(self, config, logger):
        self.config = config
        self.logger = logger
        self.history = HistoryManager(config.history_file)

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

        if start_index >= len(total_blocks):
            self.logger.info("File already finished.")
            self.history.mark_done(file_path)
            return

        i = start_index
        while i < len(total_blocks):
            chunk = total_blocks[i : i + self.config.initial_batch]

            if not bot.send_batch(page, chunk):
                self.logger.warn("Send failed. Waiting for manual fix...")
                time.sleep(5)
                continue

            bot.wait_for_completion(page)

            response_text = ""
            if self.config.auto_copy:
                response_text = bot.fetch_latest_response(page)

            if not response_text.strip():
                self.logger.warn("Auto-copy failed. Waiting for manual copy...")
                input("Copy translation then press Enter...")
                response_text = pyperclip.paste()

            srt_matches = TranscriptParser.extract_srt_blocks(response_text)
            required = max(1, int(len(chunk) * self.config.min_match_ratio))
            if len(srt_matches) >= required:
                with open(out_path, "a", encoding="utf-8-sig") as handle:
                    if i > 0:
                        handle.write("\n\n")
                    handle.write("\n\n".join(srt_matches))
                i += len(chunk)
                self.logger.info("Batch saved.")
            else:
                self.logger.warn(
                    f"Mismatch ({len(srt_matches)}/{len(chunk)}). Trying manual paste."
                )
                input("Paste translation then press Enter...")
                manual_text = pyperclip.paste()
                matches = TranscriptParser.extract_srt_blocks(manual_text)
                if matches:
                    with open(out_path, "a", encoding="utf-8-sig") as handle:
                        if i > 0:
                            handle.write("\n\n")
                        handle.write("\n\n".join(matches))
                    i += len(chunk)
                else:
                    self.logger.warn("Manual paste still failed. Retrying batch...")
                    time.sleep(2)

        self.history.mark_done(file_path)
        self.logger.info(f"Completed {os.path.basename(out_path)}")


class EagleGUI:
    def __init__(self, config):
        self.config = config
        self.root = tk.Tk()
        self.root.title("🦅 Eagle V110 - Subtitle Keeper")
        self.root.geometry("900x600")
        self.processor = EagleProcessor(config, EagleLog(self._log))
        self.files = []
        self.selected_dir = tk.StringVar()
        self.auto_copy_var = tk.BooleanVar(value=config.auto_copy)
        self.batch_var = tk.IntVar(value=config.initial_batch)
        self.status_queue = queue.Queue()
        self.worker_thread = None
        self._build_ui()

    def _build_ui(self):
        header = ttk.Frame(self.root, padding=10)
        header.pack(fill="x")

        ttk.Label(header, text="🦅 Eagle V110", font=("Segoe UI", 16, "bold")).pack(
            side="left"
        )
        ttk.Label(header, text="Smart Subtitle Translator", font=("Segoe UI", 10)).pack(
            side="left", padx=10
        )

        controls = ttk.Frame(self.root, padding=10)
        controls.pack(fill="x")

        ttk.Button(controls, text="📂 Choose Folder", command=self._choose_folder).pack(
            side="left"
        )
        ttk.Label(controls, textvariable=self.selected_dir).pack(
            side="left", padx=8
        )
        ttk.Label(controls, text="Batch Size").pack(side="left", padx=(20, 4))
        ttk.Spinbox(controls, from_=5, to=100, textvariable=self.batch_var, width=5).pack(
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
        self.tree.column("status", width=150)
        self.tree.column("path", width=600)
        self.tree.pack(fill="both", expand=True)

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
        self.files = self.processor.get_all_files(self.selected_dir.get())
        for file_path in self.files:
            status = self.processor.build_status(file_path)
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
        self.config.initial_batch = self.batch_var.get()
        self.config.auto_copy = self.auto_copy_var.get()
        self.worker_thread = threading.Thread(target=self._run_processing, daemon=True)
        self.worker_thread.start()
        self._log("Launch sequence initiated.")
        self.root.after(200, self._poll_status)

    def _stop(self):
        messagebox.showinfo("Stop", "Close the app to stop the process safely.")

    def _run_processing(self):
        logger = EagleLog(self.status_queue.put)
        self.processor = EagleProcessor(self.config, logger)
        bot = GeminiBot(self.config, logger)
        playwright, browser, page = bot.connect()
        try:
            self.processor.process(self.files, bot, page)
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

    def _log(self, message):
        self.log_box.configure(state="normal")
        self.log_box.insert("end", message + "\n")
        self.log_box.configure(state="disabled")
        self.log_box.see("end")

    def run(self):
        self.root.mainloop()


def run_cli(config):
    logger = EagleLog(print)
    processor = EagleProcessor(config, logger)
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
    parser = argparse.ArgumentParser(description="Eagle V110 - Subtitle Translator")
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
        initial_batch=args.batch,
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
