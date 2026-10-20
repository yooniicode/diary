#!/usr/bin/env python3
"""취준일기 타임랩스 자동화.

흐름:
  1. %USERPROFILE%\\Videos\\Timelapse 에서 OBS 녹화 파일(YYYY-MM-DD hh-mm-ss.mkv)을 찾는다.
  2. 아직 처리 안 된 파일을 날짜별로 묶는다. 같은 날짜의 파일은 모두 모아 다시 인코딩한다
     (하루에 여러 번 녹화해도 결과는 항상 "그날 전체" 하나).
  3. ffmpeg 로 이어붙이고 배속을 올려 720p 무음 mp4 로 만든다.
     배속은 결과가 약 TARGET_SECONDS 가 되도록 자동 결정(최소 MIN_SPEED 배).
  4. GitHub Release(태그 = 날짜)에 mp4 와 썸네일 jpg 를 올린다.
  5. _posts/YYYY-MM-DD-timelapse.md 를 만들고(기존 본문은 보존) commit & push 한다.
  6. 처리한 파일명을 상태 파일에 기록한다.

상태/로그: %LOCALAPPDATA%\\DiaryTimelapse\\{state.json, daily.log}
"""
from __future__ import annotations

import argparse
import glob
import json
import logging
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

GH_REPO = "yooniicode/diary"
REC_DIR = Path.home() / "Videos" / "Timelapse"
REPO_DIR = Path(__file__).resolve().parent.parent
POSTS_DIR = REPO_DIR / "_posts"
APP_DIR = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "DiaryTimelapse"
STATE_FILE = APP_DIR / "state.json"
LOG_FILE = APP_DIR / "daily.log"

TARGET_SECONDS = 150      # 결과 영상 목표 길이
MIN_SPEED = 15            # 최소 배속 (2fps 녹화 기준 15배 = 30fps 로 프레임 전부 사용)
OUT_FPS = 30
OUT_WIDTH = 1280
SETTLE_SECONDS = 180      # 마지막 수정 후 이 시간 안 지난 파일은 "녹화 중"으로 보고 건너뜀
PRUNE_DAYS = 30           # 처리 완료된 원본 mkv 보관 기간
TZ = "+0900"

FNAME_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}) \d{2}-\d{2}-\d{2}\.mkv$")

log = logging.getLogger("diary")


# ---------- 유틸 ----------

def setup_logging(verbose: bool) -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
    fh.setFormatter(fmt)
    log.addHandler(fh)
    if verbose or (sys.stdout is not None and sys.stdout.isatty()):
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        log.addHandler(sh)
    log.setLevel(logging.INFO)


def find_tool(name: str) -> str:
    """ffmpeg/ffprobe 를 PATH 또는 winget 설치 위치에서 찾는다."""
    p = shutil.which(name)
    if p:
        return p
    local = os.environ.get("LOCALAPPDATA", "")
    candidates = [
        os.path.join(local, "Microsoft", "WinGet", "Links", f"{name}.exe"),
        *glob.glob(os.path.join(local, "Microsoft", "WinGet", "Packages",
                                "Gyan.FFmpeg*", "ffmpeg-*", "bin", f"{name}.exe")),
    ]
    for c in candidates:
        if os.path.isfile(c):
            return c
    raise FileNotFoundError(f"{name} 을 찾을 수 없음 (winget install Gyan.FFmpeg)")


def run(cmd: list[str], cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess:
    log.debug("RUN %s", " ".join(cmd))
    cp = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                        encoding="utf-8", errors="replace")
    if check and cp.returncode != 0:
        raise RuntimeError(f"command failed ({cp.returncode}): {' '.join(cmd)}\n{cp.stderr[-2000:]}")
    return cp


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {"processed": {}}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------- 녹화 파일 수집 ----------

def is_in_use(f: Path) -> bool | None:
    """Windows: 다른 프로세스(OBS)가 파일을 열어 두고 있으면 True.
    공유 모드 0 으로 열어 보고 ERROR_SHARING_VIOLATION(32)이면 사용 중.
    판단 불가(비 Windows 등)면 None."""
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.windll.kernel32
    k32.CreateFileW.restype = wintypes.HANDLE
    GENERIC_READ, OPEN_EXISTING = 0x80000000, 3
    INVALID = wintypes.HANDLE(-1).value
    h = k32.CreateFileW(str(f), GENERIC_READ, 0, None, OPEN_EXISTING, 0, None)
    if h == INVALID:
        err = ctypes.get_last_error() or k32.GetLastError()
        return True if err == 32 else None
    k32.CloseHandle(h)
    return False


def list_recordings() -> dict[str, list[Path]]:
    """날짜 -> 녹화 파일 목록 (녹화 중인 파일 제외)."""
    by_date: dict[str, list[Path]] = {}
    if not REC_DIR.exists():
        return by_date
    now = time.time()
    for f in sorted(REC_DIR.glob("*.mkv")):
        m = FNAME_RE.match(f.name)
        if not m:
            continue
        st = f.stat()
        if st.st_size == 0:
            continue
        # 1순위: 파일 잠금(OBS 가 아직 쓰는 중). 2순위(판단 불가 시): 최근 수정 시각.
        in_use = is_in_use(f)
        busy = in_use if in_use is not None else (now - st.st_mtime < SETTLE_SECONDS)
        if busy:
            log.info("skip (아직 녹화 중): %s", f.name)
            continue
        by_date.setdefault(m.group(1), []).append(f)
    return by_date


def probe_duration(ffprobe: str, f: Path) -> float:
    cp = run([ffprobe, "-v", "error", "-show_entries", "format=duration",
              "-of", "default=noprint_wrappers=1:nokey=1", str(f)])
    try:
        return float(cp.stdout.strip())
    except ValueError:
        return 0.0


# ---------- 인코딩 ----------

def encode_day(ffmpeg: str, ffprobe: str, date: str, files: list[Path], work: Path) -> dict:
    total = sum(probe_duration(ffprobe, f) for f in files)
    if total <= 0:
        raise RuntimeError(f"{date}: 녹화 길이를 읽을 수 없음")
    speed = max(MIN_SPEED, math.ceil(total / TARGET_SECONDS))

    listfile = work / f"{date}.txt"
    listfile.write_text("".join(f"file '{f.as_posix()}'\n" for f in files), encoding="utf-8")
    out_mp4 = work / f"{date}.mp4"
    poster = work / f"{date}.jpg"

    vf = f"setpts=PTS/{speed},fps={OUT_FPS},scale={OUT_WIDTH}:-2:flags=lanczos,format=yuv420p"
    base = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
            "-f", "concat", "-safe", "0", "-i", str(listfile), "-an", "-vf", vf]
    encoders = [
        ["-c:v", "h264_nvenc", "-preset", "p5", "-rc", "vbr", "-cq", "28", "-b:v", "0"],
        ["-c:v", "libx264", "-preset", "medium", "-crf", "26"],
    ]
    last_err = None
    for enc in encoders:
        cp = run(base + enc + ["-movflags", "+faststart", str(out_mp4)], check=False)
        if cp.returncode == 0 and out_mp4.exists() and out_mp4.stat().st_size > 0:
            log.info("encoded with %s", enc[1])
            break
        last_err = cp.stderr[-1500:]
        log.warning("encoder %s failed, trying next: %s", enc[1], last_err)
    else:
        raise RuntimeError(f"{date}: 인코딩 실패\n{last_err}")

    out_dur = probe_duration(ffprobe, out_mp4)
    run([ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
         "-ss", f"{max(0.0, out_dur * 0.3):.2f}", "-i", str(out_mp4),
         "-frames:v", "1", "-q:v", "3", str(poster)])
    return {
        "mp4": out_mp4, "poster": poster,
        "recorded_minutes": int(round(total / 60)),
        "output_seconds": int(round(out_dur)),
        "speed": speed, "clips": len(files),
        "size_mb": round(out_mp4.stat().st_size / 1e6, 1),
    }


# ---------- GitHub Release ----------

def upload_release(date: str, mp4: Path, poster: Path, info: dict) -> tuple[str, str]:
    exists = run(["gh", "release", "view", date, "-R", GH_REPO], check=False).returncode == 0
    notes = (f"{date} 취준일기 타임랩스\n\n"
             f"- 녹화 {info['recorded_minutes']}분 → {info['output_seconds']}초 ({info['speed']}배속)\n"
             f"- 클립 {info['clips']}개, {info['size_mb']} MB")
    if not exists:
        run(["gh", "release", "create", date, "-R", GH_REPO,
             "--title", f"{date} 취준일기", "--notes", notes])
    else:
        run(["gh", "release", "edit", date, "-R", GH_REPO, "--notes", notes])
    # 에셋 이름은 날짜로 고정 → URL 이 항상 같음. --clobber 로 재실행 시 덮어씀.
    run(["gh", "release", "upload", date, str(mp4), str(poster), "-R", GH_REPO, "--clobber"])
    base = f"https://github.com/{GH_REPO}/releases/download/{date}"
    return f"{base}/{mp4.name}", f"{base}/{poster.name}"


# ---------- Jekyll 포스트 ----------

def write_post(date: str, video_url: str, poster_url: str, info: dict) -> Path:
    POSTS_DIR.mkdir(parents=True, exist_ok=True)
    path = POSTS_DIR / f"{date}-timelapse.md"
    body = "\n"
    if path.exists():
        parts = path.read_text(encoding="utf-8").split("---", 2)
        if len(parts) == 3:
            body = parts[2]          # 사용자가 직접 쓴 본문은 보존
    fm = "\n".join([
        "---",
        f'title: "{date} 취준일기"',
        f"date: {date} 23:50:00 {TZ}",
        f"video: {video_url}",
        f"poster: {poster_url}",
        f"recorded_minutes: {info['recorded_minutes']}",
        f"output_seconds: {info['output_seconds']}",
        f"speed: {info['speed']}",
        f"clips: {info['clips']}",
        "---",
    ])
    path.write_text(fm + body, encoding="utf-8", newline="\n")
    return path


def git_publish(dates: list[str]) -> None:
    run(["git", "pull", "--rebase", "--autostash", "--quiet"], cwd=REPO_DIR, check=False)
    run(["git", "add", "_posts"], cwd=REPO_DIR)
    if run(["git", "diff", "--cached", "--quiet"], cwd=REPO_DIR, check=False).returncode == 0:
        log.info("git: 변경 없음")
        return
    run(["git", "commit", "-q", "-m", f"diary: {', '.join(dates)}"], cwd=REPO_DIR)
    run(["git", "push", "-q"], cwd=REPO_DIR)
    log.info("git: pushed %s", dates)


# ---------- 정리 ----------

def prune_old(state: dict) -> None:
    cutoff = time.time() - PRUNE_DAYS * 86400
    for name in list(state["processed"]):
        f = REC_DIR / name
        if f.exists() and f.stat().st_mtime < cutoff:
            try:
                f.unlink()
                log.info("pruned %s", name)
            except OSError as e:
                log.warning("prune failed %s: %s", name, e)


# ---------- main ----------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--date", help="이 날짜(YYYY-MM-DD)만 강제로 다시 만든다")
    ap.add_argument("--dry-run", action="store_true", help="계획만 출력")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    setup_logging(args.verbose)

    try:
        ffmpeg, ffprobe = find_tool("ffmpeg"), find_tool("ffprobe")
    except FileNotFoundError as e:
        log.error("%s", e)
        return 2

    state = load_state()
    by_date = list_recordings()
    if args.date:
        targets = {args.date: by_date.get(args.date, [])}
        if not targets[args.date]:
            log.error("%s 녹화 파일 없음 (%s)", args.date, REC_DIR)
            return 1
    else:
        targets = {d: fs for d, fs in by_date.items()
                   if any(f.name not in state["processed"] for f in fs)}

    if not targets:
        log.info("처리할 녹화 없음")
        prune_old(state)
        return 0

    for d, fs in sorted(targets.items()):
        log.info("%s: %d clip(s) %s", d, len(fs), [f.name for f in fs])
    if args.dry_run:
        return 0

    done: list[str] = []
    work = Path(tempfile.mkdtemp(prefix="diary-", dir=APP_DIR))
    try:
        for d, fs in sorted(targets.items()):
            try:
                info = encode_day(ffmpeg, ffprobe, d, fs, work)
                log.info("%s: %s", d, {k: v for k, v in info.items() if k not in ("mp4", "poster")})
                video_url, poster_url = upload_release(d, info["mp4"], info["poster"], info)
                write_post(d, video_url, poster_url, info)
                for f in fs:
                    state["processed"][f.name] = d
                save_state(state)
                done.append(d)
            except Exception:
                log.exception("%s 처리 실패", d)
        if done:
            try:
                git_publish(done)
            except Exception:
                log.exception("git publish 실패 (포스트 파일은 남아 있음, 다음 실행 때 같이 push 됨)")
    finally:
        shutil.rmtree(work, ignore_errors=True)
    prune_old(state)
    return 0 if done else 1


if __name__ == "__main__":
    sys.exit(main())
