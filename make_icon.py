"""生成程序图标 app_icon.ico：蓝色圆角底 + 白色飞机剪影（朝上=北，便于跟随模式按航向旋转）。"""

from PIL import Image, ImageDraw

S = 256
img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(img)

# 蓝色圆角背景（透明圆角）
margin = 16
d.rounded_rectangle([margin, margin, S - margin, S - margin], radius=52,
                    fill=(45, 127, 249, 255))

# 飞机剪影（顶视图，机头朝上）。坐标基于 100x100 盒，中心 (50,50)，再放大 2.56 倍。
pts = [
    (50, 4),    # 机头
    (54, 34),   # 机身右侧（翼根前）
    (92, 52),   # 右翼尖
    (60, 52),   # 右翼后缘
    (56, 62),   # 右平尾根
    (78, 76),   # 右平尾尖
    (50, 68),   # 机尾中心
    (22, 76),   # 左平尾尖
    (44, 62),   # 左平尾根
    (40, 52),   # 左翼后缘
    (8, 52),    # 左翼尖
    (46, 34),   # 机身左侧（翼根前）
]
sc = 2.56
plane = [(x * sc, y * sc) for (x, y) in pts]
d.polygon(plane, fill=(255, 255, 255, 255))

# 机头到翼根的轻微描边，增强辨识度
d.line(plane + [plane[0]], fill=(225, 235, 250, 255), width=2, joint="curve")

img.save(
    "app_icon.ico",
    sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
)
print("saved app_icon.ico")
