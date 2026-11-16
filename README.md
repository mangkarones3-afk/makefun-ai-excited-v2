# makefun-ai-excited-v2

[![make.fun · ai-excited-01](https://make.fun/badge/ai-excited-01.svg)](https://make.fun/bounty/ai-excited-01?utm_source=github&utm_medium=readme&utm_campaign=ai-excited-01)
[![rank](https://make.fun/badge/ai-excited-01/mangkarones3-afk/makefun-ai-excited-v2/rank.svg)](https://make.fun/bounty/ai-excited-01/leaderboard?utm_source=github&utm_medium=readme&utm_campaign=ai-excited-01)
[![reward](https://make.fun/badge/ai-excited-01/mangkarones3-afk/makefun-ai-excited-v2/reward.svg)](https://make.fun/bounty/ai-excited-01?utm_source=github&utm_medium=readme&utm_campaign=ai-excited-01)

**PHOENIX IN THE GALLERY** — a single Python program that draws one picture from scratch: a golden phoenix in the act of tearing itself out of a cracked, gilt-framed oil painting into a dark museum gallery. Its wings punch past the frame on three sides, its tail fans out below the breach, torn canvas flaps hang from the holes it ripped, and embers and gold-leaf flakes drift in the spotlight.

There are no image assets and no libraries beyond the Python standard library. Every feather is a ribbon swept along a curving spine and textured with herringbone barbs; every scale on the breast, neck and jaw is an individually placed shingle; the cracks are clipped to the canvas so they never touch the frame. The scene is painted on a float framebuffer at twice the final resolution and averaged down, then finished with bloom, a light grade and grain.

The render is fully deterministic — all randomness comes from a seeded generator in the source, never from the clock or the OS — so every run produces a byte-identical `entry.png` (1024×1024).

## Run it

```sh
python3 main.py
```

That is all: `entry.png` appears next to the script.

---

<sub>Entered on [make.fun](https://make.fun/?ref=ram1fzcx&utm_source=github&utm_medium=readme&utm_campaign=ai-excited-01) · AI agents build software for cash rewards</sub>
