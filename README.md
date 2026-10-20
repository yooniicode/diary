# 취준일기 (diary.yooniicode.site)

OBS로 2fps 녹화한 화면을 매일 밤 타임랩스로 묶어 올리는 Jekyll 사이트.

- 영상 파일: 이 레포의 GitHub Releases (태그 = 날짜)
- 글: `_posts/YYYY-MM-DD-timelapse.md` (자동 생성, 본문은 자유롭게 편집 가능)
- 자동화 스크립트: `tools/daily.py` — Windows 작업 스케줄러가 매일 23:50에 실행
- 스케줄러 등록: `tools/install_task.ps1`

수동 실행:

    python tools/daily.py            # 미처리 녹화 전부 처리
    python tools/daily.py --date 2026-10-20   # 특정 날짜 다시 만들기
    python tools/daily.py --dry-run  # 인코딩/업로드 없이 계획만 출력
