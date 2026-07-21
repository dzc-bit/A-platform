from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

OUT = Path(r"D:\shixi\report_diagrams")
OUT.mkdir(exist_ok=True)

FONT_CANDIDATES = [
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
    r"C:\Windows\Fonts\simsun.ttc",
]
FONT = next((p for p in FONT_CANDIDATES if Path(p).exists()), None)


def font(size, bold=False):
    if FONT:
        try:
            return ImageFont.truetype(FONT, size, index=1 if bold else 0)
        except Exception:
            return ImageFont.truetype(FONT, size)
    return ImageFont.load_default()


TITLE = font(34, True)
LABEL = font(24, True)
TEXT = font(20)
SMALL = font(17)
BG = "#f7faf8"
INK = "#24433d"
TEAL = "#0f766e"
BLUE = "#3366a8"
GOLD = "#b7791f"
LINE = "#70958b"


def canvas(title):
    im = Image.new("RGB", (1600, 980), BG)
    d = ImageDraw.Draw(im)
    d.text((60, 36), title, fill=INK, font=TITLE)
    return im, d


def box(d, xy, text, fill="#e8f3ef", outline=TEAL, f=LABEL):
    x1, y1, x2, y2 = xy
    d.rounded_rectangle(xy, radius=14, fill=fill, outline=outline, width=3)
    lines = text.split("\n")
    heights = [d.textbbox((0, 0), line, font=f)[3] for line in lines]
    total = sum(heights) + (len(lines) - 1) * 7
    y = (y1 + y2 - total) / 2
    for line, h in zip(lines, heights):
        w = d.textbbox((0, 0), line, font=f)[2]
        d.text(((x1 + x2 - w) / 2, y), line, fill=INK, font=f)
        y += h + 7


def arrow(d, start, end, color=LINE, width=4):
    d.line([start, end], fill=color, width=width)
    import math
    a = math.atan2(end[1] - start[1], end[0] - start[0])
    r = 14
    p1 = (end[0] - r * math.cos(a - 0.5), end[1] - r * math.sin(a - 0.5))
    p2 = (end[0] - r * math.cos(a + 0.5), end[1] - r * math.sin(a + 0.5))
    d.polygon([end, p1, p2], fill=color)


def save_architecture():
    im, d = canvas("图3-1  东软智慧商务AI助手平台系统架构")
    box(d, (70, 210, 360, 370), "企业用户\n客服人员\n管理员 / 决策者", fill="#edf5f2")
    box(d, (490, 180, 830, 400), "Vue 3 + Vite前端\n路由 / Pinia / SSE\n问答 / 知识 / 工单 / 看板", fill="#e8f1fb", outline=BLUE)
    box(d, (960, 180, 1290, 400), "FastAPI统一API\n认证与角色授权\n业务服务 / OpenAPI", fill="#fff5df", outline=GOLD)
    box(d, (490, 570, 830, 790), "LangGraph编排\n分类 + 混合RAG\n白名单工具 + 质检 + 回退", fill="#e9f3ed")
    box(d, (960, 570, 1290, 790), "SQLite演示库\nRedis缓存\n知识索引 / 会话 / 工单", fill="#f0edf9", outline="#7657a8")
    box(d, (1330, 250, 1530, 360), "OpenAI-\ncompatible", fill="#f7ece8", outline="#a14d34", f=SMALL)
    box(d, (1330, 600, 1530, 710), "Dify\n可选网关", fill="#f7ece8", outline="#a14d34", f=SMALL)
    arrow(d, (360, 290), (490, 290)); arrow(d, (830, 290), (960, 290)); arrow(d, (1125, 400), (1125, 570)); arrow(d, (830, 680), (960, 680)); arrow(d, (1290, 260), (1330, 305), color="#a14d34"); arrow(d, (1290, 680), (1330, 655), color="#a14d34")
    im.save(OUT / "architecture.png")


def save_modules():
    im, d = canvas("图3-2  平台功能模块结构")
    box(d, (580, 150, 1020, 280), "东软智慧商务AI助手平台", fill="#dcefe9")
    groups = [(100, 410, "用户入口", "登录注册\n智能对话\n会话与偏好"), (430, 410, "知识与客服", "知识库\n检索测试\n工单与转人工"), (760, 410, "管理与分析", "用户管理\nAI设置 / 审计\n运营看板"), (1090, 410, "AI基础能力", "混合RAG\nLangGraph\nDify / 缓存 / 回退")]
    for x, y, title, content in groups:
        box(d, (x, y, x + 260, y + 260), title + "\n" + content, fill="#f4f8f6", outline=TEAL, f=SMALL)
        arrow(d, (800, 280), (x + 130, 410))
    box(d, (360, 800, 1240, 900), "角色边界：企业用户 | 客服人员 | 系统管理员 | 决策者", fill="#fff5df", outline=GOLD, f=TEXT)
    im.save(OUT / "modules.png")


def save_er():
    im, d = canvas("图3-3  核心实体E-R关系图")
    box(d, (90, 160, 390, 340), "users\n用户、角色、状态", fill="#e8f1fb", outline=BLUE, f=TEXT)
    box(d, (610, 130, 990, 300), "conversations\n会话与标题", fill="#e8f1fb", outline=BLUE, f=TEXT)
    box(d, (1210, 160, 1510, 340), "messages\n消息、引用、轨迹", fill="#e8f1fb", outline=BLUE, f=TEXT)
    box(d, (90, 580, 390, 760), "user_preferences\n风格、语言、朗读", fill="#e9f3ed", f=SMALL)
    box(d, (610, 560, 990, 780), "knowledge_documents\n原文、状态、版本\nknowledge_chunks索引片段", fill="#f0edf9", outline="#7657a8", f=SMALL)
    box(d, (1210, 560, 1510, 780), "support_tickets\n问题、优先级、状态\n建议回复 / 最终回复", fill="#fff5df", outline=GOLD, f=SMALL)
    arrow(d, (390, 250), (610, 215)); arrow(d, (990, 215), (1210, 250)); arrow(d, (220, 340), (220, 580)); arrow(d, (390, 670), (610, 670)); arrow(d, (990, 670), (1210, 670))
    for x, y, t in [(455, 185, "1:N"), (1050, 185, "1:N"), (245, 445, "1:1"), (500, 650, "文档索引"), (1040, 650, "用户工单")]:
        d.text((x, y), t, fill=TEAL, font=SMALL)
    im.save(OUT / "er.png")


def save_flow():
    im, d = canvas("图4-1  智能问答与回退流程")
    steps = ["接收问题", "Token与参数校验", "分类与检索并行", "白名单工具路由", "生成带证据回答", "质量检查", "SSE输出 / 保存消息"]
    x0, y = 70, 430
    for i, s in enumerate(steps):
        x = x0 + i * 215
        box(d, (x, y, x + 175, y + 130), s, fill="#e8f3ef", f=SMALL)
        if i < len(steps) - 1: arrow(d, (x + 175, y + 65), (x + 215, y + 65))
    box(d, (290, 680, 660, 830), "无证据 / 高风险\n转人工并说明限制", fill="#fff5df", outline=GOLD, f=SMALL)
    box(d, (910, 680, 1280, 830), "模型超时 / Dify失败\n本地RAG/Agent回退", fill="#f7ece8", outline="#a14d34", f=SMALL)
    arrow(d, (700, 495), (475, 680), color=GOLD); arrow(d, (1125, 560), (1100, 680), color="#a14d34")
    im.save(OUT / "flow.png")


def save_classes():
    im, d = canvas("图4-2  核心类与服务对象关系")
    box(d, (90, 180, 430, 480), "AuthService\n+ issue_token()\n+ require_role()\n+ get_current_user()", fill="#e8f1fb", outline=BLUE, f=SMALL)
    box(d, (580, 180, 940, 480), "KnowledgeService\n+ ingest_document()\n+ hybrid_search()\n+ rebuild_index()", fill="#f0edf9", outline="#7657a8", f=SMALL)
    box(d, (1090, 180, 1510, 480), "BusinessAgentOrchestrator\n+ classify()\n+ retrieve()\n+ quality_check()", fill="#e9f3ed", f=SMALL)
    box(d, (300, 650, 680, 860), "TicketService\n+ create_ticket()\n+ save_draft()\n+ publish_event()", fill="#fff5df", outline=GOLD, f=SMALL)
    box(d, (930, 650, 1310, 860), "DifyGateway / CacheService\n+ call_remote()\n+ fallback()\n+ build_cache_key()", fill="#f7ece8", outline="#a14d34", f=SMALL)
    arrow(d, (430, 330), (580, 330)); arrow(d, (940, 330), (1090, 330)); arrow(d, (710, 480), (490, 650)); arrow(d, (1300, 480), (1120, 650)); arrow(d, (680, 755), (930, 755))
    im.save(OUT / "classes.png")


if __name__ == "__main__":
    save_architecture(); save_modules(); save_er(); save_flow(); save_classes()
    print(OUT)
