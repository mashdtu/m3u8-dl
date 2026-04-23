#!/usr/bin/env python3
import os
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import urlparse

import questionary
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


def find_m3u8_urls(page_url: str, want: str = "master") -> tuple[list[str], list]:
    m3u8_urls = []
    raw_cookies = []

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
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

        stop = threading.Event()
        t = threading.Thread(target=loading_spinner, args=("Loading page...", stop), daemon=True)
        t.start()
        try:
            with page.expect_request(lambda r: want in r.url and ".m3u8" in r.url, timeout=30000):
                pass
        except Exception:
            pass
        finally:
            stop.set()
            t.join()

        raw_cookies = context.cookies()

        browser.close()

    return list(dict.fromkeys(m3u8_urls)), raw_cookies


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
            choices.append(questionary.Choice(title="best (automatic)", value="bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best"))
            fmt = questionary.select("Select quality:", choices=choices).ask()
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
            m3u8_url,
        ]
        subprocess.run(cmd)
    finally:
        os.unlink(tf.name)


if __name__ == "__main__":
    page_url = sys.argv[1].replace("\\", "") if len(sys.argv) > 1 else None
    output = sys.argv[2] if len(sys.argv) > 2 else None

    if not page_url:
        page_url = questionary.text("Page URL:").ask()
        if not page_url:
            sys.exit(0)
        page_url = page_url.replace("\\", "")

    if not output:
        output = questionary.text("Output filename:", default="video.mp4").ask()
        if not output:
            sys.exit(0)

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

    urls, cookies = find_m3u8_urls(page_url, want=stream_pref if stream_pref != "ask" else "master")
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

    print(f"Downloading to {output}...")
    download(chosen, output, cookies)
    print("Done.")