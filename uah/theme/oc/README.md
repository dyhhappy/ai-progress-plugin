# OC 资源（uah/theme/oc）

UAH 里的 OC 只作为**品牌印章 / 状态小贴章**出现（不是桌宠、不是主视觉、不替代状态灯或 `UAH` 字样）。

## 谁说了算

| 内容 | Source of Truth |
|---|---|
| 角色长相/气质/配色/异色瞳/发型/服装 | **用户提供的 OC 原始设定图** |
| 摆放位置、尺寸、附着方式 | **UI 概念图** |

因此本目录下的素材**全部由脚本从原始设定图裁切生成**：
**零重绘、零改色、零加表情、零加符号**。

## 文件

```
source/oc_sheet.jpg      用户提供的原图（仅作生成输入，UI 不读取它）
build_stamps.py          生成脚本（Pillow；只在离线生成时用，UI 运行不依赖 Pillow）
poses/oc_pose_*.png      5 个姿态的原始裁切（待机/行走/工作/思考/摸鱼）
oc_stamp.png             标准印章（= 待机姿态，默认回落实）
oc_stamp_<state>.png     状态印章（128px）
oc_stamp_<state>_256.png 同上的 256px 变体（高 DPI 用）
stamps.json              生成清单：状态 → 姿态 → 文件
```

## 状态槽 → 姿态映射（不做六套新美术）

| 状态槽 | 使用的原画姿态 | 说明 |
|---|---|---|
| `idle` | 待机 | 标准印章 |
| `starting` | 行走 | 启动/重试中 |
| `working` | 工作 | RUNNING |
| `thinking` | 思考 | 思考中 |
| `waiting` | 思考 | 等待输入/审批（**复用**思考，不新画） |
| `warning` | 待机 | 告警感由 **UI 边框/文案**承担，角色本身不改造 |
| `completed` | 行走 | 完成 |
| `paused` | 摸鱼 | 被暂停 |

## 重新生成

```bash
python uah/theme/oc/build_stamps.py                  # 用默认 source/oc_sheet.jpg
python uah/theme/oc/build_stamps.py path/to/new.jpg   # 换一张原图
```

包围盒是**测量**出来的（列/行投影 + 与背景色的距离），不是估的；
换图后需要重新测量并更新 `POSES` 常量。

## 缺素材时会怎样

`uah/ui/oc.py` 会渲染一个中性「OC」占位章并打印一行提示。
这是刻意的：宁可留白，也不擅自"重画一个长得像的白毛萌妹"。
