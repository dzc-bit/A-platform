from __future__ import annotations

import math
import shutil
import zipfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


ROOT = Path(r"D:\shixi")
OUT_DIR = ROOT / "report-figures-optimized"
SOURCE_DOCX = ROOT / "实训报告.docx"
OUTPUT_DOCX = ROOT / "实训报告_绘图优化版.docx"
FONT_PATH = Path(r"C:\Windows\Fonts\msyh.ttc")

BLACK = (35, 35, 35)
GRAY = (105, 105, 105)
WHITE = (255, 255, 255)
LIGHT = (248, 248, 248)


def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(FONT_PATH, size, index=1 if bold else 0)


def canvas(size: tuple[int, int]) -> tuple[Image.Image, ImageDraw.ImageDraw]:
    image = Image.new("RGB", size, WHITE)
    return image, ImageDraw.Draw(image)


def center_text(draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int], lines: list[str], size: int = 24, bold: bool = False, fill=BLACK, spacing: int = 7) -> None:
    x1, y1, x2, y2 = box
    f = font(size, bold)
    heights = [draw.textbbox((0, 0), line, font=f)[3] for line in lines]
    total = sum(heights) + spacing * max(0, len(lines) - 1)
    y = (y1 + y2 - total) / 2
    for line, height in zip(lines, heights):
        draw.text(((x1 + x2) / 2, y), line, font=f, fill=fill, anchor="ma")
        y += height + spacing


def title(draw: ImageDraw.ImageDraw, text: str, width: int) -> None:
    draw.text((width / 2, 34), text, font=font(28, True), fill=BLACK, anchor="ma")


def box(draw: ImageDraw.ImageDraw, rect: tuple[int, int, int, int], lines: list[str], size: int = 22, radius: int = 8, width: int = 3, fill=WHITE, bold: bool = False) -> None:
    draw.rounded_rectangle(rect, radius=radius, outline=BLACK, width=width, fill=fill)
    center_text(draw, rect, lines, size=size, bold=bold)


def ellipse(draw: ImageDraw.ImageDraw, rect: tuple[int, int, int, int], text: str, size: int = 20, width: int = 3) -> None:
    draw.ellipse(rect, outline=BLACK, width=width, fill=WHITE)
    center_text(draw, rect, [text], size=size)


def arrow(draw: ImageDraw.ImageDraw, start: tuple[int, int], end: tuple[int, int], label: str | None = None, label_pos: tuple[int, int] | None = None, width: int = 3) -> None:
    draw.line((start[0], start[1], end[0], end[1]), fill=BLACK, width=width)
    angle = math.atan2(end[1] - start[1], end[0] - start[0])
    length = 13
    spread = math.pi / 7
    p1 = (end[0] - length * math.cos(angle - spread), end[1] - length * math.sin(angle - spread))
    p2 = (end[0] - length * math.cos(angle + spread), end[1] - length * math.sin(angle + spread))
    draw.polygon([end, p1, p2], fill=BLACK)
    if label:
        x, y = label_pos or ((start[0] + end[0]) // 2, (start[1] + end[1]) // 2 - 18)
        draw.text((x, y), label, font=font(18), fill=GRAY, anchor="mm")


def actor(draw: ImageDraw.ImageDraw, x: int, y: int, label: str) -> None:
    draw.ellipse((x - 16, y - 48, x + 16, y - 16), outline=BLACK, width=3)
    draw.line((x, y - 16, x, y + 36), fill=BLACK, width=3)
    draw.line((x - 29, y + 4, x + 29, y + 4), fill=BLACK, width=3)
    draw.line((x, y + 36, x - 26, y + 72), fill=BLACK, width=3)
    draw.line((x, y + 36, x + 26, y + 72), fill=BLACK, width=3)
    draw.text((x, y + 98), label, font=font(20), fill=BLACK, anchor="ma")


def diamond(draw: ImageDraw.ImageDraw, center: tuple[int, int], width: int, height: int, lines: list[str], size: int = 20) -> tuple[int, int, int, int]:
    cx, cy = center
    points = [(cx, cy - height // 2), (cx + width // 2, cy), (cx, cy + height // 2), (cx - width // 2, cy)]
    draw.polygon(points, outline=BLACK, fill=WHITE)
    draw.line(points + [points[0]], fill=BLACK, width=3)
    center_text(draw, (cx - width // 2 + 10, cy - height // 2 + 10, cx + width // 2 - 10, cy + height // 2 - 10), lines, size=size, spacing=4)
    return (cx - width // 2, cy - height // 2, cx + width // 2, cy + height // 2)


def figure_21() -> Image.Image:
    im, d = canvas((1600, 1000))
    title(d, "图2-1  系统用例图", im.width)
    boundary = (430, 76, 1530, 930)
    d.rectangle(boundary, outline=BLACK, width=3)
    d.line((boundary[0], 126, boundary[2], 126), fill=BLACK, width=3)
    d.text((980, 101), "东软智慧商务AI助手平台", font=font(24, True), fill=BLACK, anchor="mm")
    rows = [
        (220, "企业用户", ["智能问答", "知识检索", "会话与历史", "提交咨询工单"]),
        (420, "客服人员", ["查看工单队列", "AI建议回复", "保存回复草稿", "确认结案"]),
        (620, "系统管理员", ["用户与权限管理", "知识库维护", "AI参数设置", "消息审计"]),
        (820, "决策者", ["查看运营看板", "分析服务报告"]),
    ]
    for y, actor_name, cases in rows:
        actor(d, 230, y, actor_name)
        for i, case in enumerate(cases):
            x = 600 + i * 235
            ellipse(d, (x, y - 38, x + 205, y + 38), case, size=18)
            d.line((258, y - 10, x, y), fill=BLACK, width=2)
    d.text((1450, 900), "实线表示角色与用例之间的执行关系", font=font(16), fill=GRAY, anchor="ra")
    return im


def figure_22() -> Image.Image:
    im, d = canvas((1600, 960))
    title(d, "图2-2  系统核心数据流图", im.width)
    box(d, (35, 170, 310, 270), ["企业用户 / 客服 /", "管理员 / 决策者"], size=19)
    box(d, (35, 405, 310, 505), ["Vue 3 前端", "Web 统一入口"], size=22)
    box(d, (380, 405, 670, 505), ["FastAPI API 层", "认证 / 鉴权 / 参数校验", "CRUD"], size=18)
    box(d, (735, 235, 1060, 380), ["业务服务层", "知识 / 工单 / 看板"], size=22)
    box(d, (735, 520, 1060, 665), ["AI 编排层", "分类 / 混合RAG", "Agent / LangGraph"], size=20)
    box(d, (1170, 155, 1545, 260), ["SQLite", "业务数据"], size=22)
    box(d, (1170, 365, 1545, 470), ["Redis", "缓存 / TTL"], size=22)
    box(d, (1170, 575, 1545, 680), ["FAISS + BM25", "向量与关键词索引"], size=20)
    box(d, (735, 790, 1060, 900), ["OpenAI 兼容模型 / Dify 网关", "可选增强"], size=18)
    arrow(d, (172, 270), (172, 405), "请求", (205, 340))
    arrow(d, (310, 455), (380, 455), "HTTP / JSON", (345, 432))
    arrow(d, (670, 435), (735, 310))
    arrow(d, (670, 475), (735, 585))
    arrow(d, (1060, 285), (1170, 205))
    arrow(d, (1060, 330), (1170, 415))
    arrow(d, (1060, 600), (1170, 630))
    arrow(d, (900, 790), (900, 665), "降级 / 增强", (956, 730))
    arrow(d, (1170, 445), (1060, 445))
    return im


def figure_31() -> Image.Image:
    im, d = canvas((1600, 980))
    d.text((35, 34), "图3-1  系统架构图", font=font(28, True), fill=BLACK, anchor="lm")
    d.rectangle((380, 70, 1515, 930), outline=BLACK, width=3)
    d.line((420, 70, 420, 930), fill=BLACK, width=3)
    d.text((400, 500), "系统内部", font=font(20), fill=BLACK, anchor="mm", angle=90)
    box(d, (35, 300, 295, 525), ["外部平台"], size=22, bold=True)
    box(d, (78, 350, 252, 410), ["Dify平台"], size=18)
    box(d, (78, 430, 252, 490), ["阿里云百炼服务平台"], size=16)
    box(d, (785, 72, 950, 140), ["用户"], size=20)
    box(d, (650, 135, 1110, 245), ["前端表现层", "Vue3 + Vite + 浏览器页面"], size=20)
    box(d, (605, 300, 1155, 395), ["FastAPI 接口层", "聊天接口 | 饮品记录接口 | 报表接口 | 知识管理接口"], size=18)
    box(d, (540, 465, 770, 645), ["业务服务层", "饮品记录服务", "摄入统计与报表服务", "知识采集与审核服务", "聊天服务"], size=17)
    box(d, (990, 465, 1220, 645), ["智能处理层", "LangGraph Agent 编排", "营销知识检索", "咖啡因与糖分估算", "风险判断与结果解释"], size=16)
    box(d, (590, 710, 1170, 800), ["数据访问层", "SQLAlchemy / Alembic                         Chroma Client"], size=18)
    box(d, (540, 845, 770, 925), ["SQLite 数据库"], size=19)
    box(d, (990, 845, 1220, 925), ["Chroma 知识库"], size=19)
    arrow(d, (870, 140), (870, 170))
    arrow(d, (880, 245), (880, 300), "HTTP/JSON", (940, 272))
    arrow(d, (880, 395), (655, 465))
    arrow(d, (880, 395), (1100, 465))
    arrow(d, (770, 550), (990, 550))
    arrow(d, (655, 645), (655, 710))
    arrow(d, (1100, 645), (1100, 710))
    arrow(d, (655, 800), (655, 845))
    arrow(d, (1100, 800), (1100, 845))
    arrow(d, (295, 355), (380, 355))
    arrow(d, (380, 440), (295, 440))
    return im


def figure_32() -> Image.Image:
    im, d = canvas((1600, 980))
    title(d, "图3-2  平台功能模块结构", im.width)
    box(d, (560, 90, 1040, 190), ["东软智慧商务AI助手平台"], size=26, bold=True)
    centers = [260, 600, 940, 1280]
    labels = [
        ["用户入口", "登录注册", "智能对话", "会话与偏好"],
        ["知识与客服", "知识库", "检索测试", "工单与转人工"],
        ["管理与分析", "用户管理", "AI设置 / 审计", "运营看板"],
        ["AI基础能力", "混合RAG", "LangGraph", "Dify / 缓存 / 回退"],
    ]
    for x, lines in zip(centers, labels):
        box(d, (x - 135, 310, x + 135, 590), lines, size=20)
        arrow(d, (800, 190), (x, 310))
    box(d, (380, 760, 1220, 850), ["角色边界：企业用户 | 客服人员 | 系统管理员 | 决策者"], size=20)
    return im


def figure_33() -> Image.Image:
    im, d = canvas((1600, 980))
    title(d, "图3-3  核心实体E-R关系图", im.width)
    boxes = {
        "users": (60, 160, 350, 300),
        "conversations": (650, 130, 950, 270),
        "messages": (1250, 160, 1540, 300),
        "user_preferences": (60, 615, 350, 755),
        "knowledge_documents": (650, 585, 950, 765),
        "support_tickets": (1250, 585, 1540, 765),
    }
    texts = {
        "users": ["users", "用户、角色、状态"],
        "conversations": ["conversations", "会话与标题"],
        "messages": ["messages", "消息、引用、轨迹"],
        "user_preferences": ["user_preferences", "风格、语言、朗读"],
        "knowledge_documents": ["knowledge_documents", "原文、状态、版本", "knowledge_chunks索引片段"],
        "support_tickets": ["support_tickets", "问题、优先级、状态", "建议回复 / 最终回复"],
    }
    for key, rect in boxes.items():
        box(d, rect, texts[key], size=18 if key != "knowledge_documents" else 16)
    arrow(d, (350, 230), (650, 200), "1:N", (500, 170))
    arrow(d, (950, 200), (1250, 230), "1:N", (1100, 170))
    arrow(d, (205, 300), (205, 615), "1:1", (240, 455))
    arrow(d, (350, 685), (650, 685), "文档索引", (500, 655))
    arrow(d, (950, 685), (1250, 685), "用户工单", (1100, 655))
    return im


def flow_box(draw: ImageDraw.ImageDraw, x: int, y: int, w: int, h: int, lines: list[str], size: int = 19) -> None:
    box(draw, (x, y, x + w, y + h), lines, size=size)


def figure_41() -> Image.Image:
    im, d = canvas((1000, 1180))
    title(d, "图4-1  智能问答与回退流程", im.width)
    x, w, h = 245, 510, 62
    ys = [90, 165, 240, 315, 515, 590, 770, 845]
    labels = [
        ["开始 / 接收用户问题"], ["校验 Token 与请求", "参数"], ["保存用户消息"], ["意图分类与知识检索并行"],
        ["组织带证据的回复计划"], ["质量检查"], ["发送 [DONE] 并保存", "助手消息"], ["结束"],
    ]
    for y, lines in zip(ys, labels):
        flow_box(d, x, y, w, h, lines, size=18)
    diamond(d, (500, 450), 300, 100, ["有证据", "且低风险?"])
    for a, b in zip(ys[:3], ys[1:4]):
        arrow(d, (x + w // 2, a + h), (x + w // 2, b))
    arrow(d, (x + w // 2, ys[3] + h), (x + w // 2, 400))
    arrow(d, (x + w // 2, 500), (x + w // 2, ys[4]))
    arrow(d, (x + w // 2, ys[4] + h), (x + w // 2, ys[5]))
    arrow(d, (x + w // 2, ys[5] + h), (x + w // 2, ys[6]))
    arrow(d, (x + w // 2, ys[6] + h), (x + w // 2, ys[7]))
    box(d, (35, 405, 220, 505), ["异常回退 / reset /", "转人工"], size=16)
    arrow(d, (350, 450), (220, 455), "失败", (280, 425))
    box(d, (35, 660, 220, 740), ["调用模型原生SSE流"], size=16)
    box(d, (780, 660, 965, 740), ["本地确定性回答", "(回退)"], size=16)
    arrow(d, (245, 621), (220, 700), "可用", (270, 660))
    arrow(d, (755, 621), (780, 700), "不可用", (730, 660))
    return im


def figure_42() -> Image.Image:
    im, d = canvas((1000, 1120))
    title(d, "图4-2  知识库处理流程图", im.width)
    x, w, h = 245, 510, 74
    ys = [105, 350, 455, 560, 665, 770, 875]
    labels = [
        ["上传知识文档"], ["扩展名 / 大小 /", "有效文本?"], ["段落切分 (约500字 /", "50字重叠)"], ["确定性 Embedding 输入向量化"],
        ["写入向量索引与 BM25", "关键词索引"], ["返回 ready 状态与", "分块数量"], ["结束"],
    ]
    flow_box(d, x, ys[0], w, h, labels[0], size=17)
    diamond(d, (500, 245), 290, 120, labels[1], size=17)
    for y, lines in zip(ys[2:], labels[2:]):
        flow_box(d, x, y, w, h, lines, size=17)
    arrow(d, (500, 179), (500, 185))
    for y1, y2 in zip(ys[2:-1], ys[3:]):
        arrow(d, (500, y1 + h), (500, y2))
    box(d, (760, 200, 975, 290), ["拒绝并提示"], size=18)
    arrow(d, (645, 245), (760, 245), "否", (700, 220))
    arrow(d, (500, 305), (500, ys[2]), "是", (535, 330))
    return im


def figure_43() -> Image.Image:
    im, d = canvas((1200, 760))
    title(d, "图4-3  客服工单状态流转图", im.width)
    box(d, (70, 270, 330, 400), ["open", "(企业用户提交", "问题)"], size=19)
    box(d, (435, 270, 705, 400), ["in_progress", "(客服接手 / 保存草稿)"], size=18)
    box(d, (810, 270, 1080, 400), ["resolved", "(正式确认结案)"], size=19)
    arrow(d, (330, 335), (435, 335), "创建", (382, 300))
    arrow(d, (705, 335), (810, 335), "确认结案", (758, 300))
    arrow(d, (1080, 335), (1170, 335), "闭环", (1125, 300))
    box(d, (440, 520, 700, 650), ["后台：分类 + AI建议", "回复与人工修改"], size=18)
    arrow(d, (570, 520), (570, 400), "草稿 / 修改", (640, 465))
    arrow(d, (1100, 400), (1000, 520), "通知", (1060, 460))
    box(d, (900, 520, 1130, 650), ["SSE推送", "状态更新"], size=18)
    return im


def figure_44() -> Image.Image:
    im, d = canvas((1200, 760))
    title(d, "图4-4  运营看板数据流图", im.width)
    source_rects = [(40, 120, 290, 220), (40, 285, 290, 385), (40, 450, 290, 550)]
    source_lines = [["support_tickets", "(工单表)"], ["messages", "(会话 / 引用 / 轨迹)"], ["ai_settings", "审计消息"]]
    for rect, lines in zip(source_rects, source_lines):
        box(d, rect, lines, size=18)
    box(d, (425, 285, 720, 410), ["指标聚合服务", "分类 / 统计 /", "状态检查与计算"], size=19)
    out_rects = [(845, 100, 1095, 215), (845, 280, 1095, 395), (845, 460, 1095, 575)]
    out_lines = [["运营指标看板", "(咨询量 / 工单状态)"], ["问题分类分析", "报告"], ["质检代理", "指标"]]
    for rect, lines in zip(out_rects, out_lines):
        box(d, rect, lines, size=18)
    box(d, (1120, 280, 1190, 395), ["决策者"], size=16)
    for rect in source_rects:
        arrow(d, (rect[2], (rect[1] + rect[3]) // 2), (425, 348))
    for rect in out_rects:
        arrow(d, (720, 348), (rect[0], (rect[1] + rect[3]) // 2), "可视化" if rect == out_rects[0] else None, (780, 260) if rect == out_rects[0] else None)
    arrow(d, (1095, 337), (1120, 337))
    return im


def figure_45() -> Image.Image:
    im, d = canvas((1600, 980))
    title(d, "图4-2  核心类与服务对象关系", im.width)
    rects = [
        ((80, 170, 420, 405), ["AuthService", "+ issue_token()", "+ require_role()", "+ get_current_user()"]),
        ((590, 170, 950, 405), ["KnowledgeService", "+ ingest_document()", "+ hybrid_search()", "+ rebuild_index()"]),
        ((1110, 170, 1515, 405), ["BusinessAgentOrchestrator", "+ classify()", "+ retrieve()", "+ quality_check()"]),
        ((300, 610, 680, 845), ["TicketService", "+ create_ticket()", "+ save_draft()", "+ publish_event()"]),
        ((930, 610, 1350, 845), ["DifyGateway / CacheService", "+ call_remote()", "+ fallback()", "+ build_cache_key()"]),
    ]
    for rect, lines in rects:
        box(d, rect, lines, size=20)
    arrow(d, (420, 285), (590, 285))
    arrow(d, (950, 285), (1110, 285))
    arrow(d, (770, 405), (500, 610))
    arrow(d, (1310, 405), (1140, 610))
    arrow(d, (680, 725), (930, 725))
    return im


FIGURES = {
    "image3.png": figure_21,
    "image4.png": figure_22,
    "image5.png": figure_31,
    "image6.png": figure_32,
    "image7.png": figure_33,
    "image8.png": figure_41,
    "image9.png": figure_42,
    "image10.png": figure_43,
    "image11.png": figure_44,
    "image12.png": figure_41,
    "image13.png": figure_45,
}


def main() -> None:
    OUT_DIR.mkdir(exist_ok=True)
    for name, builder in FIGURES.items():
        image = builder()
        image.save(OUT_DIR / name, dpi=(180, 180))
    temp_docx = ROOT / "report-rebuild-temp.docx"
    with zipfile.ZipFile(SOURCE_DOCX, "r") as source, zipfile.ZipFile(temp_docx, "w", zipfile.ZIP_DEFLATED) as target:
        for item in source.infolist():
            data = source.read(item.filename)
            if item.filename.startswith("word/media/") and Path(item.filename).name in FIGURES:
                data = (OUT_DIR / Path(item.filename).name).read_bytes()
            target.writestr(item, data)
    shutil.move(temp_docx, OUTPUT_DOCX)
    print(f"generated={len(FIGURES)} figures")
    print(f"output={OUTPUT_DOCX}")


if __name__ == "__main__":
    main()
