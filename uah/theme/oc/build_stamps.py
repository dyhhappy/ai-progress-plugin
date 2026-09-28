"""从 OC 原始设定图生成 UAH 用的 OC 小印章（可复跑，**不重画角色**）。

原则：
* OC 的长相一律以**原始设定图**为准 —— 本脚本只做裁切/缩放/圆角贴章底板，
  **不做任何重绘、改色、加表情、加符号**；
* UI 概念图只约束"摆放位置、大小、附着方式"。

输入：OC 原始设定图（默认取 ``source/oc_sheet.jpg``，可用参数覆盖）。
输出：``uah/theme/oc/`` 下的印章 PNG + ``stamps.json`` 清单。

包围盒不是猜的：用"与背景色的距离"做列/行投影自动定位底部 5 个 Q 版立绘。

用法：
    python uah/theme/oc/build_stamps.py [原图路径]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from PIL import Image, ImageDraw

HERE = Path(__file__).resolve().parent
DEFAULT_SRC = HERE / "source" / "oc_sheet.jpg"

#: 各 Q 版姿态的包围盒（相对 1312×1199 原图，由列/行投影测量得到，含少量余量）
POSES: dict[str, tuple[int, int, int, int]] = {
    "idle": (44, 799, 198, 1059),      # 待机 IDLE
    "walk": (237, 802, 391, 1060),     # 行走 WALK
    "work": (425, 825, 625, 1056),     # 工作 WORK（坐姿 + 桌面）
    "think": (658, 809, 808, 1057),    # 思考 THINK
    # 摸鱼 RELAX 收紧到角色本体：品牌区只有 22px，全幅横躺+猫在这个尺寸读不出人形
    "relax": (833, 830, 1002, 1044),
}

#: 主题层的状态槽 → 使用哪个姿态（复用原画姿态，不新画六套）
STATE_TO_POSE: dict[str, str] = {
    "idle": "idle",
    "working": "work",
    "thinking": "think",
    "waiting": "think",
    "warning": "idle",     # 告警感由 UI 边框/文案承担，角色本身不改造
    "completed": "walk",
    "paused": "relax",
    "starting": "walk",
}

PLATE = (242, 243, 245, 255)
PLATE_EDGE = (214, 218, 226, 255)

#: 预生成多档尺寸：运行时挑**最接近且不小于**目标的那一档再精确缩放，
#: 避免"小图放大"或"大图整数子采样"造成的块状/模糊。
#: 覆盖 逻辑 28px × DPI{1.0,1.25,1.5,2.0} 以及更大的展示场景。
SIZES = (48, 72, 96, 128, 176, 256)
#: 默认回落档（``oc_stamp.png`` 永远是它）
DEFAULT_SIZE = 128

#: 品牌印章用**半身像**：28 逻辑像素（150% 下 42px）时，全身 Q 版会糊成一团，
#: 而头像能把 OC 的关键识别点保住 —— 银白偏冷蓝头发 / 异色瞳 / 困困半睁眼 /
#: 黑白蓝服装轮廓。只做裁切，**不改画**。
#: 比例 (left, right, top, bottom)，相对该姿态的裁切框。
BUST_BOX = (0.26, 0.74, 0.00, 0.56)


def rounded_mask(size: int, radius: int) -> Image.Image:
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, size - 1, size - 1), radius=radius, fill=255)
    return mask


def make_stamp(pose: Image.Image, size: int, *, inset_ratio: float = 0.06) -> Image.Image:
    """贴章：圆角浅色底板 + 居中姿态图（保持比例，不拉伸）。"""
    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    plate = Image.new("RGBA", (size, size), PLATE)
    edge = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    ImageDraw.Draw(edge).rounded_rectangle((0, 0, size - 1, size - 1),
                                           radius=int(size * 0.24), outline=PLATE_EDGE, width=1)
    canvas.paste(plate, (0, 0), rounded_mask(size, int(size * 0.24)))
    canvas.alpha_composite(edge)

    inset = int(size * inset_ratio)
    inner = size - 2 * inset
    w, h = pose.size
    scale = min(inner / w, inner / h)
    new = (max(1, int(w * scale)), max(1, int(h * scale)))
    canvas.alpha_composite(pose.resize(new, Image.LANCZOS).convert("RGBA"),
                           (inset + (inner - new[0]) // 2, inset + (inner - new[1]) // 2))
    return canvas


def main(argv: list[str]) -> int:
    src = Path(argv[1]) if len(argv) > 1 else DEFAULT_SRC
    if not src.is_file():
        print(f"找不到原图：{src}", file=sys.stderr)
        return 2
    sheet = Image.open(src).convert("RGB")
    print(f"原图 {src.name} {sheet.size}")

    manifest: dict[str, object] = {
        "note": "由 build_stamps.py 从 OC 原始设定图裁切生成；未做任何重绘/改色",
        "source": src.name,
        "source_size": list(sheet.size),
        "poses": {k: list(v) for k, v in POSES.items()},
        "state_to_pose": STATE_TO_POSE,
        "bust_box": list(BUST_BOX),
        "kinds": {"bust": "品牌印章（半身像，小尺寸可辨识）",
                  "full": "全身姿态（较大展示场景）"},
        "files": [],
    }

    (HERE / "source").mkdir(parents=True, exist_ok=True)
    poses_dir = HERE / "poses"
    poses_dir.mkdir(exist_ok=True)

    cut: dict[str, Image.Image] = {}
    for name, box in POSES.items():
        img = sheet.crop(box).convert("RGB")
        img.save(poses_dir / f"oc_pose_{name}.png")
        cut[name] = img

    def bust_of(pose: Image.Image) -> Image.Image:
        w, h = pose.size
        l, r, t, b = BUST_BOX
        return pose.crop((int(w * l), int(h * t), int(w * r), int(h * b)))

    for state, pose_name in STATE_TO_POSE.items():
        pose = cut[pose_name]
        # 品牌印章（半身像）：文件名前缀 oc_bust
        bust = bust_of(pose)
        for size in SIZES:
            name = ("oc_bust.png" if state == "idle" else f"oc_bust_{state}.png")
            out = (HERE / (name if size == DEFAULT_SIZE
                           else name.replace(".png", f"_{size}.png")))
            make_stamp(bust, size).save(out)
            manifest["files"].append({"file": out.name, "state": state,
                                      "pose": pose_name, "size": size, "kind": "bust"})
        for size in SIZES:
            if size == DEFAULT_SIZE:
                out = (HERE / "oc_stamp.png" if state == "idle"
                       else HERE / f"oc_stamp_{state}.png")
            else:
                out = (HERE / f"oc_stamp_{size}.png" if state == "idle"
                       else HERE / f"oc_stamp_{state}_{size}.png")
            make_stamp(pose, size).save(out)
            manifest["files"].append({"file": out.name, "state": state,
                                      "pose": pose_name, "size": size,
                                      "kind": "full"})

    (HERE / "stamps.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"生成 {len(manifest['files'])} 个印章文件 + stamps.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
