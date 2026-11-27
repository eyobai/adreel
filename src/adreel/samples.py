"""Synthetic sample media so the engine can be exercised without real footage.

The clips deliberately cover the awkward cases: 60 fps portrait, 25 fps landscape with no
audio track, and "speech" with pauses (tone bursts) for silence-cut experiments.
"""

from __future__ import annotations

from pathlib import Path

from adreel.render.ffmpeg import FFmpeg


def generate_samples(ff: FFmpeg, out_dir: Path, fonts_dir: Path) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    # ffmpeg runs inside the fonts dir and names the font file bare: a Windows path
    # ("C:\\...") in a filter argument would need fragile colon/backslash escaping.
    font = "NotoSans-Bold.ttf"
    cwd = fonts_dir.resolve()
    out_dir = out_dir.resolve()
    paths = {
        "c1": out_dir / "c1_portrait_60fps.mp4",
        "c2": out_dir / "c2_landscape_noaudio.mp4",
        "c3": out_dir / "c3_landscape_25fps.mp4",
        "logo": out_dir / "logo.png",
        "vo": out_dir / "vo_test.wav",
    }

    def label(text: str, size: int) -> str:
        return (
            f"drawtext=fontfile='{font}':text='{text}':fontsize={size}:fontcolor=white:"
            f"box=1:boxcolor=black@0.5:boxborderw=12:x=(w-tw)/2:y=h*0.12"
        )

    def timecode(size: int) -> str:
        return (
            f"drawtext=fontfile='{font}':text='%{{pts\\:hms}}':fontsize={size}:fontcolor=yellow:"
            f"x=(w-tw)/2:y=h*0.88"
        )

    # "speech": 1.3 s tone, 0.7 s silence, repeating
    speech = "aevalsrc='0.4*sin(2*PI*220*t)*lt(mod(t\\,2)\\,1.3)':s=48000:d={d}"

    ff.run(
        cwd=cwd,
        args=[
            "-f",
            "lavfi",
            "-i",
            "testsrc2=s=1080x1920:r=60:d=10",
            "-f",
            "lavfi",
            "-i",
            speech.format(d=10),
            "-vf",
            f"drawbox=x='200+150*sin(t)':y=700:w=300:h=300:color=orange@0.9:t=fill,"
            f"{label('CLIP 1 portrait 60fps', 56)},{timecode(64)}",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-shortest",
            str(paths["c1"]),
        ],
    )
    ff.run(
        cwd=cwd,
        args=[
            "-f",
            "lavfi",
            "-i",
            "smptehdbars=s=1280x720:r=30:d=8",
            "-vf",
            f"drawbox=x='mod(t*200\\,1100)':y=300:w=180:h=180:color=red@0.9:t=fill,"
            f"{label('CLIP 2 landscape no audio', 40)},{timecode(48)}",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-pix_fmt",
            "yuv420p",
            str(paths["c2"]),
        ],
    )
    ff.run(
        cwd=cwd,
        args=[
            "-f",
            "lavfi",
            "-i",
            "testsrc=s=1920x1080:r=25:d=12",
            "-f",
            "lavfi",
            "-i",
            speech.format(d=12),
            "-vf",
            f"{label('CLIP 3 landscape 25fps', 64)},{timecode(72)}",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-shortest",
            str(paths["c3"]),
        ],
    )
    ff.run(
        cwd=cwd,
        args=[
            "-f",
            "lavfi",
            "-i",
            "color=c=0x1F7A4D:s=400x400:d=1",
            "-vf",
            "format=rgba,geq=r='r(X,Y)':g='g(X,Y)':b='b(X,Y)':"
            "a='if(lte(hypot(X-200,Y-200),196),255,0)',"
            f"drawtext=fontfile='{font}':text='LOGO':fontsize=110:fontcolor=white:"
            "x=(w-tw)/2:y=(h-th)/2",
            "-frames:v",
            "1",
            str(paths["logo"]),
        ],
    )
    # Stand-in "voiceover" (higher-pitched bursts) to hear the ducking of the original audio.
    ff.run(
        cwd=cwd,
        args=[
            "-f",
            "lavfi",
            "-i",
            "aevalsrc='0.5*sin(2*PI*660*t)*lt(mod(t\\,1.5)\\,1.0)':s=48000:d=9",
            "-ac",
            "2",
            str(paths["vo"]),
        ],
    )
    return list(paths.values())
