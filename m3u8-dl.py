#!/usr/bin/env python3
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import urlparse

import questionary
from prompt_toolkit.formatted_text import FormattedText
from tqdm import tqdm
from playwright.sync_api import sync_playwright


def loading_spinner(label: str, stop_event: threading.Event):
    with tqdm(bar_format="{desc} {elapsed}", desc=label) as bar:
        while not stop_event.is_set():
            bar.update(0)
            time.sleep(0.2)


def label_for_url(url: str) -> str:
    path = urlparse(url).path
    name = path.rstrip("/").split("/")[-1]
    if "master" in name:
        return f"[master]  {name}  (camera/video)"
    if "index" in name:
        return f"[index]   {name}  (slides/screen)"
    return f"[stream]  {name}"


def find_m3u8_urls(page_url: str, want: str = "master", headed: bool = False) -> tuple[list[str], list, str]:
    m3u8_urls = []
    raw_cookies = []
    page_title = ""

    timeout_ms = 120_000 if headed else 30_000

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=not headed,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = browser.new_context(
            user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )
        page = context.new_page()
        page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")

        def on_request(req):
            if ".m3u8" in req.url:
                m3u8_urls.append(req.url)

        page.on("request", on_request)

        page.goto(page_url, wait_until="domcontentloaded")

        if headed:
            print("Browser opened. Log in if prompted, then wait for the video to start loading.")

        stop = threading.Event()
        label = "Waiting for stream (log in if needed)..." if headed else "Loading page..."
        t = threading.Thread(target=loading_spinner, args=(label, stop), daemon=True)
        t.start()
        try:
            with page.expect_request(lambda r: want in r.url and ".m3u8" in r.url, timeout=timeout_ms):
                pass
        except Exception:
            pass
        finally:
            stop.set()
            t.join()

        raw_cookies = context.cookies()
        try:
            page_title = page.title()
        except Exception:
            pass

        browser.close()

    return list(dict.fromkeys(m3u8_urls)), raw_cookies, page_title


def list_formats(m3u8_url: str, cookie_file: str) -> list[tuple[str, str]]:
    result = subprocess.run(
        ["yt-dlp", "--cookies", cookie_file, "-F", m3u8_url],
        capture_output=True, text=True,
    )
    formats = []
    for line in result.stdout.splitlines():
        parts = line.split()
        if parts and parts[0].isdigit():
            fmt_id = parts[0]
            label = " ".join(parts[1:])
            formats.append((fmt_id, label))
    return formats


def download(m3u8_url: str, output: str, cookies: list):
    tf = tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False)
    try:
        tf.write("# Netscape HTTP Cookie File\n")
        for c in cookies:
            domain = c.get("domain", "")
            flag = "TRUE" if domain.startswith(".") else "FALSE"
            path = c.get("path", "/")
            secure = "TRUE" if c.get("secure") else "FALSE"
            expiry = str(int(c.get("expires") or 0))
            tf.write(f"{domain}\t{flag}\t{path}\t{secure}\t{expiry}\t{c['name']}\t{c['value']}\n")
        tf.close()

        formats = list_formats(m3u8_url, tf.name)
        if formats:
            choices = [questionary.Choice(title=label, value=fmt_id) for fmt_id, label in formats]
            best_fmt_id = max(formats, key=lambda f: (lambda m: int(m.group(1)) * int(m.group(2)) if m else 0)(re.search(r'(\d+)x(\d+)', f[1])))[0]
            fmt = questionary.select(
                "Select quality:",
                choices=choices,
                default=best_fmt_id,
                style=questionary.Style([
                    ("highlighted", "noinherit"),
                    ("selected", "noinherit"),
                    ("pointer", "noinherit"),
                    ("question", "noinherit bold"),
                    ("answer", "noinherit bold fg:#ffaf00"),
                    ("instruction", "noinherit"),
                ]),
            ).ask()
            if not fmt:
                return
        else:
            fmt = "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best"

        cmd = [
            "yt-dlp",
            "--cookies", tf.name,
            "--format", fmt,
            "--output", output,
            "--no-part",
            "--quiet",
            "--progress",
            "--print", "after_move:filepath",
            m3u8_url,
        ]
        subprocess.run(cmd)
    finally:
        os.unlink(tf.name)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--headed", "-H", action="store_true", help="Open a visible browser (for pages requiring login)")
    args, remaining = parser.parse_known_args()

    page_url = remaining[0].replace("\\", "") if len(remaining) > 0 else None
    output = remaining[1] if len(remaining) > 1 else None

    if not page_url:
        page_url = questionary.text("Page URL:").ask()
        if not page_url:
            sys.exit(0)
        page_url = page_url.replace("\\", "")

    stream_pref = questionary.select(
        "Which stream?",
        choices=[
            questionary.Choice("Camera/video  (master)", value="master"),
            questionary.Choice("Slides/screen (index)", value="index"),
            questionary.Choice("Ask me after loading", value="ask"),
        ]
    ).ask()
    if not stream_pref:
        sys.exit(0)

    if not args.headed:
        args.headed = questionary.confirm("Does this page require login?", default=False).ask()

    urls, cookies, page_title = find_m3u8_urls(page_url, want=stream_pref if stream_pref != "ask" else "master", headed=args.headed)

    if not output:
        default_name = page_title.strip() if page_title.strip() else "video"
        _placeholder = FormattedText([("italic fg:ansibrightblack", default_name)])
        output = questionary.text(
            "Output filename:",
            placeholder=_placeholder,
            style=questionary.Style([("answer", "bold fg:#ffaf00")]),
        ).ask()
        if output is None:
            sys.exit(0)
        if not output:
            output = default_name
            qmark    = "\033[38;2;95;129;157m?\033[0m"
            question = "\033[1mOutput filename:\033[0m"
            answer   = f"\033[1m\033[38;2;255;175;0m{output}\033[0m"
            sys.stdout.write(f"\033[1A\r\033[2K{qmark} {question} {answer}\n")

    if not output.endswith(".mp4"):
        output += ".mp4"

    if os.path.exists(output):
        overwrite = questionary.confirm(f"'{output}' already exists. Overwrite?", default=False).ask()
        if not overwrite:
            sys.exit(0)
    if not urls:
        print("No .m3u8 URLs found.")
        sys.exit(1)

    if stream_pref == "ask":
        if len(urls) == 1:
            chosen = urls[0]
            print(f"Found: {label_for_url(chosen)}")
        else:
            choices = [questionary.Choice(title=label_for_url(u), value=u) for u in urls]
            chosen = questionary.select("Select stream to download:", choices=choices).ask()
            if not chosen:
                sys.exit(0)
    else:
        chosen = next((u for u in urls if stream_pref in u), urls[0])
        print(f"Selected: {label_for_url(chosen)}")

    print(f"Pending download to {output}...")
    saved = download(chosen, output, cookies) or output
    stream_label = "camera/video" if "master" in chosen else "slides/screen"
    size_str = ""
    try:
        size = os.path.getsize(saved)
        if size >= 1_073_741_824:
            size_str = f"  ({size / 1_073_741_824:.2f} GB)"
        elif size >= 1_048_576:
            size_str = f"  ({size / 1_048_576:.1f} MB)"
        else:
            size_str = f"  ({size / 1024:.0f} KB)"
    except OSError:
        pass
    print(f"\nDownload complete.")
    print(f"  Stream : {stream_label}")
    print(f"  Saved  : {saved}{size_str}\n")