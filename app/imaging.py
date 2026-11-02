"""图像预处理与本地切题算法（纯 OpenCV / NumPy，无网络依赖）。

处理链路：
1. 文档轮廓检测 + 透视校正（拍到斜的卷子也能拉正）；
2. 轻微倾斜校正；
3. 墨迹掩码（自适应二值化）+ 批改笔迹掩码（红/蓝笔 HSV 分离）；
4. 行投影分析 → 依据"行间距突变"把整页切成题目块，支持双栏版面；
5. 每块计算红笔痕迹密度、批改符号形态，输出"疑似错题"评分供用户确认。

设计原则：算法只做"候选框 + 置信度"，最终由用户点选确认，
因此宁可多给几个框，也不做过度裁剪。
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Iterable

import cv2
import numpy as np
from PIL import Image, ImageFilter

from .config import MAX_WORK_EDGE


@dataclass
class Candidate:
    x: int
    y: int
    w: int
    h: int
    wrong_score: float = 0.0
    red_density: float = 0.0
    mark_count: int = 0
    mark_kinds: str = ""
    column: int = 0
    has_number: bool = False
    kind: str = "block"

    def as_dict(self) -> dict:
        return asdict(self)


def pil_to_cv(img: Image.Image) -> np.ndarray:
    arr = np.array(img.convert("RGB"))
    return cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)


def cv_to_pil(arr: np.ndarray) -> Image.Image:
    return Image.fromarray(cv2.cvtColor(arr, cv2.COLOR_BGR2RGB))


def _resize_for_work(gray: np.ndarray, max_edge: int = MAX_WORK_EDGE) -> tuple[np.ndarray, float]:
    h, w = gray.shape[:2]
    longest = max(h, w)
    if longest <= max_edge:
        return gray, 1.0
    scale = max_edge / float(longest)
    return cv2.resize(gray, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA), scale


# --------------------------------------------------------------------------- #
# 透视校正与去倾斜
# --------------------------------------------------------------------------- #

def find_document_corners(gray: np.ndarray) -> np.ndarray | None:
    """在灰度图上寻找最大四边形纸面轮廓，失败返回 None。"""
    h, w = gray.shape[:2]
    blur = cv2.GaussianBlur(gray, (5, 5), 0)
    edges = cv2.Canny(blur, 50, 150)
    edges = cv2.dilate(edges, np.ones((3, 3), np.uint8), iterations=1)
    contours, _ = cv2.findContours(edges, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    area_img = float(h * w)
    best: np.ndarray | None = None
    best_area = 0.0
    for c in sorted(contours, key=cv2.contourArea, reverse=True)[:8]:
        area = cv2.contourArea(c)
        if area < area_img * 0.25:
            continue
        peri = cv2.arcLength(c, True)
        approx = cv2.approxPolyDP(c, 0.02 * peri, True)
        if len(approx) == 4 and cv2.isContourConvex(approx):
            if area > best_area:
                best, best_area = approx.reshape(4, 2), area
    return best


def order_corners(pts: np.ndarray) -> np.ndarray:
    """按 左上/右上/右下/左下 排序。"""
    pts = np.asarray(pts, dtype=np.float32).reshape(4, 2)
    s = pts.sum(axis=1)
    d = np.diff(pts, axis=1).reshape(-1)
    ordered = np.zeros((4, 2), dtype=np.float32)
    ordered[0] = pts[np.argmin(s)]
    ordered[2] = pts[np.argmax(s)]
    ordered[1] = pts[np.argmin(d)]
    ordered[3] = pts[np.argmax(d)]
    return ordered


def warp_document(img: np.ndarray, corners: np.ndarray) -> tuple[np.ndarray, bool]:
    rect = order_corners(corners)
    (tl, tr, br, bl) = rect
    width = max(np.linalg.norm(br - bl), np.linalg.norm(tr - tl))
    height = max(np.linalg.norm(tr - br), np.linalg.norm(tl - bl))
    if width < 200 or height < 200:
        return img, False
    dst = np.array(
        [[0, 0], [width - 1, 0], [width - 1, height - 1], [0, height - 1]], dtype=np.float32
    )
    matrix = cv2.getPerspectiveTransform(rect, dst)
    out = cv2.warpPerspective(img, matrix, (int(width), int(height)), flags=cv2.INTER_AREA)
    if out.shape[0] < 200 or out.shape[1] < 200:
        return img, False
    return out, True


def deskew(gray: np.ndarray) -> tuple[np.ndarray, float]:
    """基于水平投影方差的倾斜校正。

    做法：在小图上以 0.4° 步长旋转，取"行投影跳变最剧烈"的角度。
    文字行越水平，行与行之间的空白带越干净，投影的一阶差分方差越大。
    比 minAreaRect 稳得多（后者会被内容分布带偏）。
    """
    small, _scale = _resize_for_work(gray, 900)
    thr = cv2.adaptiveThreshold(
        small, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY_INV, 31, 12
    )
    h, w = thr.shape[:2]
    if h < 60 or w < 60:
        return gray, 0.0

    def score_of(angle: float) -> float:
        m = cv2.getRotationMatrix2D((w / 2, h / 2), angle, 1.0)
        rot = cv2.warpAffine(thr, m, (w, h), flags=cv2.INTER_NEAREST, borderValue=0)
        proj = rot.sum(axis=1).astype(np.float64)
        if proj.size < 3:
            return 0.0
        return float(np.var(np.diff(proj)))

    base = score_of(0.0)
    best_angle, best_score = 0.0, base
    for angle in np.arange(-6.0, 6.01, 0.4):
        s = score_of(float(angle))
        if s > best_score:
            best_score, best_angle = s, float(angle)
    if abs(best_angle) < 0.4 or best_score < base * 1.06:
        return gray, 0.0
    # 在最大角度附近做一次细化
    fine = best_angle
    for angle in np.arange(best_angle - 0.3, best_angle + 0.31, 0.1):
        s = score_of(float(angle))
        if s > best_score:
            best_score, fine = s, float(angle)
    best_angle = fine

    H, W = gray.shape[:2]
    m = cv2.getRotationMatrix2D((W / 2, H / 2), best_angle, 1.0)
    out = cv2.warpAffine(gray, m, (W, H), flags=cv2.INTER_CUBIC, borderValue=255)
    return out, float(best_angle)


# --------------------------------------------------------------------------- #
# 掩码
# --------------------------------------------------------------------------- #

def ink_mask(gray: np.ndarray) -> np.ndarray:
    """墨迹掩码：自适应阈值 + 形态学清理，去掉纸张纹理噪点。"""
    thr = cv2.adaptiveThreshold(
        gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY_INV, 35, 15
    )
    # 去掉孤立噪点，但保留细笔画
    thr = cv2.morphologyEx(thr, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8))
    return thr


def marking_mask(bgr: np.ndarray) -> np.ndarray:
    """批改笔迹掩码：红笔为主，兼容蓝笔（教师改卷常用色）。"""
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    h, s, v = cv2.split(hsv)
    red = (((h <= 12) | (h >= 168)) & (s >= 60) & (v >= 45)).astype(np.uint8) * 255
    blue = ((h >= 95) & (h <= 135) & (s >= 70) & (v >= 40)).astype(np.uint8) * 255
    mask = cv2.bitwise_or(red, blue)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8))
    mask = cv2.dilate(mask, np.ones((3, 3), np.uint8), iterations=1)
    return mask


def _classify_mark(mask: np.ndarray) -> str:
    """粗略判断单个批改笔迹的形态：对勾 / 叉号 / 划线 / 其他。"""
    m = (mask > 0).astype(np.uint8)
    if m.sum() < 12:
        return "other"
    d1 = float(np.mean(m[:-1, :-1] & m[1:, 1:])) if m.shape[0] > 2 else 0.0
    d2 = float(np.mean(m[:-1, 1:] & m[1:, :-1])) if m.shape[0] > 2 else 0.0
    orth = float(np.mean(m[:-1, :] & m[1:, :])) + float(np.mean(m[:, :-1] & m[:, 1:]))
    h, w = m.shape[:2]
    aspect = w / float(h) if h else 0.0
    if d1 > 0.12 and d2 > 0.12:
        return "cross"
    if (d1 > 0.15 or d2 > 0.15) and orth < 0.55:
        return "check"
    if aspect > 3.0:
        return "underline"
    return "other"


def mark_stats(mark: np.ndarray, box: tuple[int, int, int, int]) -> tuple[int, float, list[str]]:
    """统计指定框内的批改笔迹数量、密度与形态。"""
    x, y, w, h = box
    x0, y0 = max(0, x), max(0, y)
    sub = mark[y0 : y0 + h, x0 : x0 + w]
    if sub.size == 0:
        return 0, 0.0, []
    density = float(np.count_nonzero(sub)) / float(sub.size)
    num, labels, stats, _ = cv2.connectedComponentsWithStats((sub > 0).astype(np.uint8), 8)
    kinds: list[str] = []
    count = 0
    for i in range(1, num):
        comp_w, comp_h, comp_area = stats[i, cv2.CC_STAT_WIDTH], stats[i, cv2.CC_STAT_HEIGHT], stats[i, cv2.CC_STAT_AREA]
        if comp_area < 25 or comp_w < 4 or comp_h < 4:
            continue
        if comp_w > w * 0.95 and comp_h > h * 0.8:
            continue
        count += 1
        comp_mask = (labels == i).astype(np.uint8)
        ys, xs = np.where(comp_mask)
        if xs.size == 0:
            continue
        crop = comp_mask[ys.min() : ys.max() + 1, xs.min() : xs.max() + 1]
        kinds.append(_classify_mark(crop))
    return count, density, kinds


def estimate_char_height(ink: np.ndarray) -> int:
    """估计字高，用于推导"段间距"阈值。"""
    num, _labels, stats, _ = cv2.connectedComponentsWithStats(ink, 8)
    heights = []
    h_img = ink.shape[0]
    for i in range(1, min(num, 4000)):
        h = stats[i, cv2.CC_STAT_HEIGHT]
        a = stats[i, cv2.CC_STAT_AREA]
        if 3 <= h <= h_img * 0.08 and a >= 8:
            heights.append(int(h))
    if not heights:
        return max(10, h_img // 60)
    return int(np.clip(np.median(heights), 8, 120))


# --------------------------------------------------------------------------- #
# 版面切分
# --------------------------------------------------------------------------- #

def _runs(mask_vec: np.ndarray) -> list[tuple[int, int]]:
    """把一维布尔数组转换为 [start, end) 区间列表。"""
    idx = np.flatnonzero(mask_vec)
    if idx.size == 0:
        return []
    breaks = np.flatnonzero(np.diff(idx) > 1)
    starts = np.concatenate(([idx[0]], idx[breaks + 1]))
    ends = np.concatenate((idx[breaks], [idx[-1]]))
    return [(int(s), int(e) + 1) for s, e in zip(starts, ends)]


def detect_columns(ink: np.ndarray, char_h: int) -> list[tuple[int, int]]:
    """检测双栏版面，返回每栏的 (x0, x1)。"""
    h, w = ink.shape[:2]
    col_ink = ink.sum(axis=0) / 255.0
    active = col_ink > max(1.0, h * 0.004)
    gaps = [g for g in _runs(~active) if (g[1] - g[0]) >= max(18, w * 0.035)]
    if not gaps:
        return [(0, w)]

    def side_profile(side: np.ndarray) -> tuple[float, int]:
        rows = (side.sum(axis=1) / 255.0) > 2
        fraction = float(rows.mean()) if rows.size else 0.0
        changes = np.diff(rows.astype(np.int8))
        bands = int(np.count_nonzero(changes == 1)) + (1 if rows.size and rows[0] else 0)
        return fraction, bands

    # 只接受落在中部、两侧都像"正文栏"的空白带
    mid_gaps = [g for g in gaps if 0.3 * w < (g[0] + g[1]) / 2 < 0.7 * w]
    for g in sorted(mid_gaps, key=lambda t: t[1] - t[0], reverse=True):
        left = ink[:, : g[0]]
        right = ink[:, g[1] :]
        if left.size == 0 or right.size == 0:
            continue
        lf, lb = side_profile(left)
        rf, rb = side_profile(right)
        if lf > 0.10 and rf > 0.10 and lb >= 3 and rb >= 3:
            return [(0, g[0]), (g[1], w)]
    return [(0, w)]


def _title_split_row(block: np.ndarray, char_h: int, col_width: int) -> int:
    """识别"整行都是大字"的标题行，返回该行下沿（相对 y），否则 0。

    试卷顶部往往是「XX 中学 XX 考试」这类居中大标题，字号明显大于正文。
    只在该行至少有 4 个高度相近的大字、整体居中时才判定为标题，
    避免把带大括号/大分数的公式行误切。
    """
    num, _labels, stats, _ = cv2.connectedComponentsWithStats(block, 8)
    if num < 6:
        return 0
    # 只考虑"比正文大一点"的字：过高的（红色叉号、大括号）排除
    big = [
        i
        for i in range(1, num)
        if 1.3 * char_h < stats[i, cv2.CC_STAT_HEIGHT] < 2.3 * char_h
    ]
    if len(big) < 4:
        return 0
    tops = [stats[i, cv2.CC_STAT_TOP] for i in big]
    tops_sorted = sorted(tops)
    # 按 top 聚类，取最靠上且成员足够多的一组
    groups: list[list[int]] = [[tops_sorted[0]]]
    for t in tops_sorted[1:]:
        if t - groups[-1][-1] <= char_h * 0.6:
            groups[-1].append(t)
        else:
            groups.append([t])
    cluster = max(groups, key=len)
    if len(cluster) < 4:
        return 0
    members = [i for i in big if min(cluster) <= stats[i, cv2.CC_STAT_TOP] <= max(cluster)]
    row_top = min(stats[i, cv2.CC_STAT_TOP] for i in members)
    row_end = max(stats[i, cv2.CC_STAT_TOP] + stats[i, cv2.CC_STAT_HEIGHT] for i in members)
    if row_top > char_h * 1.5:
        return 0
    if row_end > block.shape[0] * 0.55:
        return 0
    lefts = [stats[i, cv2.CC_STAT_LEFT] for i in members]
    if min(lefts) < col_width * 0.05:
        return 0
    return int(row_end)


def _segment_region(
    ink: np.ndarray,
    mark: np.ndarray,
    x0: int,
    x1: int,
    gap_threshold: int,
    min_height: int,
    column_index: int,
    char_h: int,
) -> list[Candidate]:
    region = ink[:, x0:x1]
    h = region.shape[0]
    row_ink = region.sum(axis=1) / 255.0
    active = row_ink > max(1.0, (x1 - x0) * 0.002)
    bands = _runs(active)
    if not bands:
        return []
    # 合并间距小于阈值的行带（同一题内的换行）
    merged: list[list[int]] = [list(bands[0])]
    for s, e in bands[1:]:
        if s - merged[-1][1] <= gap_threshold:
            merged[-1][1] = e
        else:
            merged.append([s, e])

    out: list[Candidate] = []
    for index, (s, e) in enumerate(merged):
        if e - s < min_height:
            continue
        # 首个行带里若含居中大标题，先把标题切出来单独成块
        if index == 0 and column_index == 0:
            block_probe = ink[s:e, x0:x1]
            cut = _title_split_row(block_probe, char_h, x1 - x0)
            if cut > 0:
                title = _make_candidate(ink, mark, x0, x1, s, s + cut, column_index, "title")
                if title:
                    out.append(title)
                s = s + cut
                if e - s < min_height:
                    continue
        cand = _make_candidate(ink, mark, x0, x1, s, e, column_index, "block")
        if cand:
            out.append(cand)
    return out


def _make_candidate(
    ink: np.ndarray,
    mark: np.ndarray,
    x0: int,
    x1: int,
    s: int,
    e: int,
    column_index: int,
    kind: str,
) -> Candidate | None:
    region = ink[:, x0:x1]
    band = region[s:e]
    if band.size == 0:
        return None
    cols = band.sum(axis=0) / 255.0
    nz = np.flatnonzero(cols > max(0.5, (e - s) * 0.01))
    if nz.size == 0:
        return None
    left = int(nz[0]) + x0
    right = int(nz[-1]) + x0 + 1
    pad_x = max(4, int((right - left) * 0.01))
    bx0 = max(0, left - pad_x)
    bx1 = min(ink.shape[1], right + pad_x)
    bw, bh = bx1 - bx0, e - s
    count, density, kinds = mark_stats(mark, (bx0, s, bw, bh))
    score = min(1.0, density * 42.0 + count * 0.12)
    if any(k in ("cross", "check") for k in kinds):
        score = min(1.0, score + 0.18)
    if kind == "title":
        score = 0.0
    return Candidate(
        x=bx0,
        y=s,
        w=bw,
        h=bh,
        wrong_score=round(score, 3),
        red_density=round(density, 4),
        mark_count=count,
        mark_kinds=",".join(sorted(set(kinds))),
        column=column_index,
        has_number=_looks_numbered(ink, bx0, s, bw, bh),
        kind=kind,
    )


def _looks_numbered(ink: np.ndarray, x: int, y: int, w: int, h: int) -> bool:
    """首行左侧是否像题号（1. / 12、/ (3) 这类短笔画序列）。"""
    strip_w = max(18, int(min(w * 0.12, 90)))
    strip_h = max(8, int(h * 0.45))
    sub = ink[y : y + min(strip_h, h), x : x + min(strip_w, w)]
    if sub.size == 0:
        return False
    num, _labels, stats, _ = cv2.connectedComponentsWithStats(sub, 8)
    comps = [stats[i] for i in range(1, num) if stats[i, cv2.CC_STAT_AREA] >= 6]
    if not 1 <= len(comps) <= 3:
        return False
    heights = [int(c[cv2.CC_STAT_HEIGHT]) for c in comps]
    return bool(max(heights) >= 6 and min(heights) <= 0.6 * max(heights) + 10)


# --------------------------------------------------------------------------- #
# 对外主入口
# --------------------------------------------------------------------------- #

def analyse(
    img: Image.Image,
    *,
    gap_ratio: float = 1.6,
    min_block_height: int = 26,
    detect_columns_flag: bool = True,
    correct_perspective: bool = True,
) -> dict:
    """分析整页图片，返回候选题目框（坐标为原图像素）。"""
    bgr_full = pil_to_cv(img)
    gray_full = cv2.cvtColor(bgr_full, cv2.COLOR_BGR2GRAY)

    page = None
    warped = False
    angle = 0.0
    if correct_perspective:
        corners = find_document_corners(gray_full)
        if corners is not None:
            warped_bgr, warped = warp_document(bgr_full, corners)
            if warped:
                bgr_full = warped_bgr
                gray_full = cv2.cvtColor(bgr_full, cv2.COLOR_BGR2GRAY)
                page = order_corners(corners).round(1).tolist()

    gray_work, scale = _resize_for_work(gray_full)
    bgr_work = cv2.resize(bgr_full, (gray_work.shape[1], gray_work.shape[0]), interpolation=cv2.INTER_AREA)
    gray_work, angle = deskew(gray_work)

    ink = ink_mask(gray_work)
    mark = marking_mask(bgr_work)
    char_h = estimate_char_height(ink)
    gap_threshold = max(8, int(round(char_h * gap_ratio)))
    min_h = max(8, int(min_block_height * 0.75))

    columns = detect_columns(ink, char_h) if detect_columns_flag else [(0, ink.shape[1])]
    candidates: list[Candidate] = []
    for ci, (cx0, cx1) in enumerate(columns):
        candidates.extend(
            _segment_region(ink, mark, cx0, cx1, gap_threshold, min_h, ci, char_h)
        )

    inv = 1.0 / scale if scale else 1.0
    result: list[dict] = []
    for c in candidates:
        x = int(round(c.x * inv))
        y = int(round(c.y * inv))
        w = int(round(c.w * inv))
        h = int(round(c.h * inv))
        if w < 12 or h < 10:
            continue
        c.x, c.y, c.w, c.h = x, y, w, h
        result.append(c.as_dict())
    result.sort(key=lambda d: (d["column"], d["y"], d["x"]))
    return {
        "width": img.width,
        "height": img.height,
        "page_corners": page,
        "perspective_corrected": warped,
        "deskew_angle": round(angle, 2),
        "char_height": char_h,
        "gap_threshold": gap_threshold,
        "columns": len(columns),
        "candidates": result,
        "working_width": int(ink.shape[1]),
    }


def crop(img: Image.Image, box: Iterable[float], pad_ratio: float = 0.012) -> Image.Image:
    """按框裁剪，四周留少量白边，避免公式被切掉上下标。"""
    x, y, w, h = [float(v) for v in list(box)[:4]]
    if w < 6 or h < 6:
        raise ValueError("选区太小")
    pad_x = max(6.0, w * pad_ratio)
    pad_y = max(6.0, h * pad_ratio)
    x0 = max(0, int(x - pad_x))
    y0 = max(0, int(y - pad_y))
    x1 = min(img.width, int(x + w + pad_x))
    y1 = min(img.height, int(y + h + pad_y))
    if x1 - x0 < 8 or y1 - y0 < 8:
        raise ValueError("选区太小")
    return img.crop((x0, y0, x1, y1))


def _flatten_background(gray: np.ndarray) -> np.ndarray:
    """纸底归一化：估出每一块纸的明暗，再按它反算，让整张纸变成纯白。

    手机拍/扫描的卷面从来不是均匀白：中间亮、四角暗、还有阴影和装订处的渐变。
    直接做全局对比度拉伸只能把"最亮的纸"拉到 255，其余纸仍是灰的——这就是
    上一版题图看起来"发灰、像没增强"的根因。

    做法：大核闭运算把墨迹填平，得到"没有字时的纸";再高斯模糊成平滑的光照场;
    最后 `gray / 光照场` 逐像素归一化。阴影被除掉，纸底一律 →255，墨迹保留。
    """
    h, w = gray.shape[:2]
    k = max(15, int(round(min(h, w) * 0.08)) | 1)
    bg = cv2.morphologyEx(
        gray, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k))
    )
    bg = cv2.GaussianBlur(bg, (0, 0), max(1.0, k / 3.0))
    bg = np.maximum(bg, 1)
    flat = gray.astype(np.float32) * 255.0 / bg.astype(np.float32)
    return np.clip(flat, 0, 255).astype(np.uint8)


def handwriting_mask(flat: np.ndarray) -> np.ndarray:
    """手写笔迹掩码（黑笔/铅笔也认）。

    红蓝笔靠颜色就能分，黑色手写笔没法和印刷线条比颜色，只能比**形态**：
    印刷的图线细而均匀（笔画宽 2~3px），手写圈注/划线宽 5~9px；而且手写
    只沿轮廓走一圈，填充率极低（<0.16）。

    关键：笔画宽度要看这个连通域的**整体分布**，不能只看最大值。
    一张立体几何图的轮廓线全连通，只要里面有一小段粗笔画，按 max 判断就会
    把整张图的棱线一起删掉（实测真的删过）。这里用 75 分位判断"整体是粗的"，
    印刷图（绝大多数像素是细线）永远不会满足。
    """
    ink = (flat < 150).astype(np.uint8)
    if not ink.any():
        return np.zeros(flat.shape, np.uint8)
    dist = cv2.distanceTransform(ink, cv2.DIST_L2, 5)
    num, labels, stats, _ = cv2.connectedComponentsWithStats(ink, 8)
    out = np.zeros(flat.shape, np.uint8)
    for i in range(1, num):
        x, y, w, h, area = (int(v) for v in stats[i])
        if area < 150 or w * h < 900 or max(w, h) < 40:
            continue
        sub = labels[y : y + h, x : x + w] == i
        widths = dist[y : y + h, x : x + w][sub]
        if float(np.percentile(widths, 75)) < 2.2:  # 整体偏细：印刷线条，保留
            continue
        if area / float(w * h) > 0.16:  # 填充率高：文字/实心图形，保留
            continue
        out[y : y + h, x : x + w][sub] = 255
    if out.any():
        # 外扩一点，把笔迹的软边一起盖住，否则 inpaint 会留下灰影
        out = cv2.dilate(out, np.ones((7, 7), np.uint8), iterations=1)
    return out


def _levels(x: np.ndarray, black: float, white: float) -> np.ndarray:
    """两点色阶：<=black 压成纯黑、>=white 提成纯白，中间平滑过渡（保留抗锯齿）。"""
    f = (x.astype(np.float32) - black) * (255.0 / max(1.0, white - black))
    return np.clip(f, 0, 255).astype(np.uint8)


def enhance_figure(img: Image.Image, *, upscale: float | None = None) -> Image.Image:
    """题图增强：文档级「去批注 + 纸底归一 + 线条还原」，输出干净线稿。

    整条链路（每一环都对着一种"卷面照片毛病"）：

    1. **彩色批注**（红/蓝笔）：HSV 掩码 → 外扩 → `inpaint` 补回被压住的线条。
       掩码必须外扩，半径也要够大——上一版掩码只扩 1px、半径 3，8px 宽的笔迹
       中间会留下一道黑芯，看起来就像"根本没去掉"。
    2. **黑色手写笔迹**：见 :func:`handwriting_mask`，靠笔画宽度而非颜色识别。
    3. **分档处理**：图跟图不一样，硬套一套参数必然毁掉一半的图。
       - 纸底已经很白（≥244，电子版/矢量截图）→ 只做「压黑 + 去噪」，
         这时任何光照归一把只会把线条越搞越淡；
       - 有批注、有手写 → 走全套（归一化 + 强色阶），污渍重才值得下狠手；
       - 既没脏东西、纸底又不白 → **保守**：只轻微提亮 + 轻锐化。
         这种图本来就是干净的印刷图，硬"增强"会把浅灰的球体、细虚线烧成死黑，
         还会把背面透印的文字一起放大。宁可不做，也不能越做越差。
    4. **锐化 + 小幅超采样**：小截图放大后打印才不糊。

    幂等性：整条链路是非线性的（纸底归一会把已经很白的纸继续压白），
    所以**不要对结果反复增强**——重跑时请从原始截图 + 裁剪框重新算。
    """
    rgb = np.array(img.convert("RGB"))
    bgr = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)

    # 1. 彩色批注
    mask = marking_mask(bgr)
    has_color = int((mask > 0).sum()) > 0
    if has_color:
        mask = cv2.dilate(mask, np.ones((5, 5), np.uint8), iterations=1)
        bgr = cv2.inpaint(bgr, mask, 5, cv2.INPAINT_TELEA)

    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    paper = float(np.percentile(gray, 85))

    # 2. 黑色手写笔迹（在归一化后的图上判断，条件与纸底亮度无关）
    flat = _flatten_background(gray)
    hand = handwriting_mask(flat)

    clean_sheet = paper >= 244.0  # 纸底已经是纯白：电子版、截图、矢量转图

    if clean_sheet:
        # 干净的线稿：只压黑 + 去噪。这里绝不能做光照归一，否则线条被摊薄
        work = cv2.inpaint(gray, hand, 6, cv2.INPAINT_TELEA) if hand.any() else gray
        out_gray = cv2.bilateralFilter(_levels(work, 190, 248), 5, 35, 35)
    elif has_color or hand.any():
        # 卷面照片 + 有脏东西：全套处理
        work = flat
        if hand.any():
            work = cv2.inpaint(work, hand, 6, cv2.INPAINT_TELEA)
        work = cv2.medianBlur(work, 3)
        work = cv2.bilateralFilter(work, 5, 45, 45)
        out_gray = _levels(work, 46, 226)
    else:
        # 干净的印刷图、纸底略灰：只做**全局**两点色阶把纸提到白，别把浅色内容烧黑。
        # 白点就取纸底本身，所以纸一定变 255；不用局部光照归一，
        # 那种归一化会把背面透印的淡字一起放大（实测整页文字都冒出来）。
        white = float(min(250.0, max(200.0, paper)))
        work = cv2.bilateralFilter(gray, 5, 30, 30)
        out_gray = _levels(work, 62, white)

    # 4. 锐化 + 超采样
    out = Image.fromarray(out_gray)
    if upscale is None:
        upscale = 2.0 if out.width < 800 else 1.0
    if upscale and upscale > 1.01:
        out = out.resize(
            (int(round(out.width * upscale)), int(round(out.height * upscale))),
            Image.LANCZOS,
        )
    return out.filter(ImageFilter.UnsharpMask(radius=1.0, percent=70, threshold=3))


def merge_boxes(boxes: list[dict]) -> dict:
    """把多个框合并为一个大框（用户框选跨行题目时使用）。"""
    xs0 = min(b["x"] for b in boxes)
    ys0 = min(b["y"] for b in boxes)
    xs1 = max(b["x"] + b["w"] for b in boxes)
    ys1 = max(b["y"] + b["h"] for b in boxes)
    return {"x": xs0, "y": ys0, "w": xs1 - xs0, "h": ys1 - ys0}
