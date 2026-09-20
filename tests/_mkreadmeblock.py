import base64, pathlib

block = base64.b64decode(
    "YGBgYmFzaApwb2RtYW4gYnVpbGQgLWYgZG9ja2VyL21jdS1kZXYtYmFzZS5Eb2NrZXJmaWxlIC10IG1jdS1kZXYtYmFzZSAuICAgIyDQvtC00LjQvSDRgNCw0LcsIH4xMC0yMCDQvNC40L0KcG9kbWFuIGJ1aWxkIC1mIGRvY2tlci9tY3UtZGV2LkRvY2tlcmZpbGUgLXQgbWN1LWRldgpzY3JpcHRzL2Rldi91cC5zaCAgICAgICAgICAjIG1jdS1hPTEwLjAuMy4xMCwgbWN1LWI9MTAuMC4zLjIwICjQuNC70LggaG9zdCA1MDYwLzUwNjEpCnNjcmlwdHMvZGV2L3Rlc3RfY2FsbC5zaCAgICMg0YPRgdC/0LXRgTogW2Rldl0g0JfQktCe0J3QntCaINCf0J7QlNCi0JLQldCg0JbQlNCB0J0Kc2NyaXB0cy9kZXYvZG93bi5zaApgYGAK"
).decode("utf-8")

p = pathlib.Path("README.md")
lines = p.read_text(encoding="utf-8").splitlines(keepends=True)

# Replace the empty block at index 580 (0-based) — the lone ' ' line.
idx = 580
assert lines[idx].strip() == "", f"unexpected line {idx}: {lines[idx]!r}"
lines = lines[:idx] + [block] + lines[idx + 1:]
p.write_text("".join(lines), encoding="utf-8")
print("block inserted")
