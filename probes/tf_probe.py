#!/usr/bin/env python3
"""TF 探针：枚举当前所有 frame 及其父节点。

用 tf2_ros 的 buffer 取全量 frame 列表，比逐个 tf_echo 快得多。
"""
import json
import sys

import rospy
import tf2_ros


def main():
    wait = float(sys.argv[1]) if len(sys.argv) > 1 else 3.0

    rospy.init_node("tt_tf_probe", anonymous=True, disable_signals=True)
    buffer = tf2_ros.Buffer()
    listener = tf2_ros.TransformListener(buffer)  # noqa: F841
    rospy.sleep(wait)

    try:
        frames = buffer.all_frames_as_yaml()
    except Exception as exc:
        print("RESULT " + json.dumps({"ok": False, "reason": str(exc)}))
        return

    # 解析 yaml 得到 frame -> parent 映射
    parents = {}
    current = None
    for line in frames.splitlines():
        stripped = line.strip()
        if stripped.endswith(":") and not stripped.startswith("parent"):
            current = stripped[:-1]
            parents[current] = None
        elif stripped.startswith("parent:") and current:
            parents[current] = stripped.split(":", 1)[1].strip().strip("'\"")
        elif stripped.startswith("broadcaster:") and current:
            parents.setdefault(current + "__broadcaster", []).append(
                stripped.split(":", 1)[1].strip().strip("'\""))

    clean = {k: v for k, v in parents.items() if not k.endswith("__broadcaster")}

    print("RESULT " + json.dumps({
        "ok": True,
        "frame_count": len(clean),
        "frames": clean,
    }))


if __name__ == "__main__":
    main()
