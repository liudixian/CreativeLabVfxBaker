# -*- coding: utf-8 -*-
"""
CreativeLab · DotForge  ——  位置图烘焙器 (PositionMap Baker)

把网格元素的【世界坐标】烘焙成一张 EXR 位置贴图，供 UE Niagara 当作粒子位置贴图使用。
每个纹素 = 一个粒子的位置（单位 cm，= Blender 世界坐标 x100）。

三种位置来源：
  - 网格孤岛中心 (ISLAND)：每个独立网格块算一个位置（如每颗 LED 灯珠）
  - 面中心       (FACE)  ：每个 Polygon 中心
  - 顶点         (VERTEX)：每个顶点

布局约定（与 Niagara 保持一致）：
  W = ceil(sqrt(N))，H = ceil(N / W)
  索引 i -> (col = i % W, row = i // W)，图片顶行 = 索引 0..W-1
  Niagara: u = (i % W + 0.5) / W , v = (i // W + 0.5) / H   (UE 的 V=0 在图片左上)

UE 导入贴图的必需设置：
  Compression Settings = HDR (未压缩 16F，切勿用 HDR Compressed / BC6H)
  sRGB = 关闭
  Mip Gen Settings = NoMipmaps
"""

bl_info = {
    "name": "CreativeLab · DotForge",
    "author": "CreativeLab",
    "version": (1, 0, 0),
    "blender": (4, 0, 0),
    "location": "View3D > 侧边栏 (N) > CreativeLab",
    "description": "把网格元素(顶点/面中心/网格孤岛中心)烘焙成 EXR 位置贴图，供 UE Niagara 作粒子位置贴图",
    "category": "Object",
}

import bpy
import os
import json
import csv
import math
import struct
import zlib
import bmesh
from mathutils import Vector
from bpy.props import (
    StringProperty, BoolProperty, FloatProperty, EnumProperty, PointerProperty,
)
from bpy.types import Panel, Operator, PropertyGroup


# ----------------------------------------------------------------------------
# 基础写出工具
# ----------------------------------------------------------------------------

def _save_exr(path, flat_pixels, w, h):
    """用 Blender 图像管线写出 32bit float EXR（已验证：无压缩、通道 A/B/G/R、数值无损）。"""
    img = bpy.data.images.new("dotforge_tmp", width=w, height=h, alpha=True, float_buffer=True)
    img.colorspace_settings.name = "Non-Color"
    img.pixels[:] = flat_pixels
    img.filepath_raw = path
    img.file_format = "OPEN_EXR"
    img.save()
    bpy.data.images.remove(img)
    return path


def _write_png(path, rows_u8):
    """自写 8bit PNG（无 color chunk），避免 Blender 保存时的位深/色彩空间转换。
    rows_u8: 第 0 行 = 图片顶行，每像素为 bytes/tuple。"""
    h = len(rows_u8)
    w = len(rows_u8[0])
    ch = len(rows_u8[0][0])
    raw = bytearray()
    for row in rows_u8:
        raw.append(0)  # filter = None
        for px in row:
            raw.extend(px)

    def chunk(typ, data):
        return (struct.pack(">I", len(data)) + typ + data +
                struct.pack(">I", zlib.crc32(typ + data) & 0xFFFFFFFF))

    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6 if ch == 4 else 2, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(bytes(raw), 9))
    png += chunk(b"IEND", b"")
    with open(path, "wb") as f:
        f.write(png)
    return path


# ----------------------------------------------------------------------------
# 位置采集
# ----------------------------------------------------------------------------

def gather_positions(obj, source, use_eval):
    """返回世界坐标下的位置列表 (list[Vector])。"""
    dg = None
    tmp_obj = None
    if use_eval:
        dg = bpy.context.evaluated_depsgraph_get()
        tmp_obj = obj.evaluated_get(dg)
        me = tmp_obj.to_mesh()
        M = tmp_obj.matrix_world
    else:
        me = obj.data
        M = obj.matrix_world

    try:
        pts = []
        if source == 'VERTEX':
            for v in me.vertices:
                pts.append(M @ v.co)
        elif source == 'FACE':
            for p in me.polygons:
                pts.append(M @ p.center)
        else:  # ISLAND
            bm = bmesh.new()
            bm.from_mesh(me)
            bm.verts.ensure_lookup_table()
            seen = set()
            for v in bm.verts:
                if v.index in seen:
                    continue
                stack = [v]
                seen.add(v.index)
                acc = Vector((0.0, 0.0, 0.0))
                n = 0
                while stack:
                    cur = stack.pop()
                    acc += M @ cur.co
                    n += 1
                    for e in cur.link_edges:
                        ov = e.other_vert(cur)
                        if ov.index not in seen:
                            seen.add(ov.index)
                            stack.append(ov)
                pts.append(acc / n)
            bm.free()
        return pts
    finally:
        if tmp_obj is not None:
            tmp_obj.to_mesh_clear()


# ----------------------------------------------------------------------------
# 烘焙
# ----------------------------------------------------------------------------

def bake_position_map(obj, source, use_eval, scale, out_dir, prefix,
                      square_tex, do_norm_png, do_csv, do_layout):
    pts = gather_positions(obj, source, use_eval)
    n = len(pts)
    if n == 0:
        raise RuntimeError("没有采到任何位置（对象为空？）")

    pos = [Vector(p) * scale for p in pts]  # 转 cm

    if square_tex:
        W = int(math.ceil(math.sqrt(n)))
    else:
        W = n
    W = max(W, 1)
    H = int(math.ceil(n / float(W)))

    bmin = Vector((min(p.x for p in pos), min(p.y for p in pos), min(p.z for p in pos)))
    bmax = Vector((max(p.x for p in pos), max(p.y for p in pos), max(p.z for p in pos)))
    rng = Vector((max(bmax.x - bmin.x, 1e-9),
                  max(bmax.y - bmin.y, 1e-9),
                  max(bmax.z - bmin.z, 1e-9)))

    os.makedirs(out_dir, exist_ok=True)
    files = []

    # --- EXR：纹素 RGB = 世界坐标(cm)，A = 1 ---
    rows = [[(0.0, 0.0, 0.0, 1.0) for _ in range(W)] for _ in range(H)]
    for i, p in enumerate(pos):
        rows[i // W][i % W] = (p.x, p.y, p.z, 1.0)
    flat = []
    for r in reversed(rows):          # Blender pixels 自下而上
        for px in r:
            flat.extend(px)
    exr_path = os.path.join(out_dir, "%s_%dx%d.exr" % (prefix, W, H))
    _save_exr(exr_path, flat, W, H)
    files.append(exr_path)

    # --- 归一化 PNG（0..1 反解：pos = bbox_min + rgb*(bbox_max-bbox_min)）---
    if do_norm_png:
        rows_u8 = [[(0, 0, 0, 255) for _ in range(W)] for _ in range(H)]
        for i, p in enumerate(pos):
            t = ((p.x - bmin.x) / rng.x, (p.y - bmin.y) / rng.y, (p.z - bmin.z) / rng.z)
            rows_u8[i // W][i % W] = tuple(
                int(round(min(max(c, 0.0), 1.0) * 255.0)) for c in t) + (255,)
        files.append(_write_png(os.path.join(out_dir, "%s_%dx%d_norm.png" % (prefix, W, H)), rows_u8))

    # --- 布局校验图（按 x/z 平面画点，肉眼确认排布）---
    if do_layout:
        ppm = 380.0
        pad = 8
        sx = (bmax.x - bmin.x) / scale   # 回到米
        sz = (bmax.z - bmin.z) / scale
        iw = int(math.ceil(sx * ppm)) + pad * 2
        ih = int(math.ceil(sz * ppm)) + pad * 2
        canvas = [[0] * max(iw, 1) for _ in range(max(ih, 1))]
        for p in pos:
            px_ = int(round((p.x - bmin.x) / scale * ppm)) + pad
            py_ = int(round((p.z - bmin.z) / scale * ppm)) + pad
            r = 2
            for yy in range(max(0, ih - 1 - py_ - r), min(ih, ih - py_ + r)):
                for xx in range(max(0, px_ - r), min(iw, px_ + r + 1)):
                    canvas[yy][xx] = 255
        rows_u8 = []
        for r in range(ih - 1, -1, -1):
            rows_u8.append([(canvas[r][c], canvas[r][c], canvas[r][c]) for c in range(iw)])
        files.append(_write_png(os.path.join(out_dir, prefix + "_layout_check.png"), rows_u8))

    # --- CSV ---
    if do_csv:
        csv_path = os.path.join(out_dir, "%s_positions.csv" % prefix)
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["x_cm", "y_cm", "z_cm"])
            for p in pos:
                w.writerow([round(p.x, 5), round(p.y, 5), round(p.z, 5)])
        files.append(csv_path)

    # --- info.json ---
    src_label = {'ISLAND': '网格孤岛中心', 'FACE': '面(Polygon)中心', 'VERTEX': '顶点'}[source]
    info = {
        "generated_by": "CreativeLab · DotForge",
        "source_object": obj.name,
        "position_source": source,
        "position_definition": "%s，对象矩阵变换到世界坐标后 x%g" % (src_label, scale),
        "use_evaluated_mesh": bool(use_eval),
        "position_count": n,
        "texture": {"width": W, "height": H,
                    "index_to_uv": "u=(i%W+0.5)/W, v=(i//W+0.5)/H (V 自图片顶部向下)"},
        "units": "cm (UE), = Blender 世界坐标 x%g" % scale,
        "bbox_min_cm": [round(float(v), 6) for v in bmin],
        "bbox_max_cm": [round(float(v), 6) for v in bmax],
        "ue_import_hint": {
            "Compression Settings": "HDR (不要用 HDR Compressed / BC6H，有损会偏移数值)",
            "sRGB": "off",
            "Mip Gen Settings": "NoMipmaps",
        },
        "files": [os.path.basename(f) for f in files],
    }
    with open(os.path.join(out_dir, "%s_info.json" % prefix), "w", encoding="utf-8") as f:
        json.dump(info, f, ensure_ascii=False, indent=2)

    return {"count": n, "W": W, "H": H, "dir": out_dir, "files": files,
            "bbox_min": tuple(bmin), "bbox_max": tuple(bmax)}


# ----------------------------------------------------------------------------
# 属性 / UI
# ----------------------------------------------------------------------------

class DotForgeSettings(PropertyGroup):
    target_object: PointerProperty(
        name="目标对象", type=bpy.types.Object,
        description="要烘焙位置的对象")
    position_source: EnumProperty(
        name="位置来源",
        items=[
            ('ISLAND', "网格孤岛中心", "每个独立网格块(如每颗 LED 灯珠)算一个位置"),
            ('FACE', "面中心", "每个 Polygon 的中心"),
            ('VERTEX', "顶点", "每个顶点"),
        ],
        default='ISLAND')
    use_eval: BoolProperty(
        name="应用修改器", default=True,
        description="使用求值后的网格(含几何节点/修改器结果)")
    unit_scale: FloatProperty(
        name="单位缩放", default=100.0, min=0.0001,
        description="世界坐标 -> 贴图单位的倍数。UE 用 cm，所以 = 100")
    out_dir: StringProperty(name="输出目录", subtype='DIR_PATH', default="//PositionMap")
    prefix: StringProperty(name="文件名前缀", default="posmap")
    square_tex: BoolProperty(name="方形贴图", default=True,
                             description="宽=ceil(sqrt(N))，高自动；关闭则单行")
    do_norm_png: BoolProperty(name="导出归一化 PNG", default=True)
    do_csv: BoolProperty(name="导出 CSV", default=True)
    do_layout: BoolProperty(name="导出布局校验图", default=True)


class CREATIVELAB_OT_bake_posmap(Operator):
    bl_idname = "creativelab.bake_position_map"
    bl_label = "烘焙位置图 (EXR)"
    bl_description = "把目标对象的元素位置烘焙成 EXR 位置贴图"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        s = context.scene.dotforge
        obj = s.target_object
        if obj is None:
            self.report({'ERROR'}, "请先选择目标对象")
            return {'CANCELLED'}
        if obj.type != 'MESH':
            self.report({'ERROR'}, "目标对象必须是网格 (MESH)")
            return {'CANCELLED'}

        out_dir = bpy.path.abspath(s.out_dir)
        prefix = s.prefix.strip() or obj.name.replace(" ", "_")
        try:
            r = bake_position_map(obj, s.position_source, s.use_eval,
                                  s.unit_scale, out_dir, prefix,
                                  s.square_tex, s.do_norm_png, s.do_csv, s.do_layout)
        except Exception as e:
            self.report({'ERROR'}, "烘焙失败：%s" % e)
            return {'CANCELLED'}

        self.report({'INFO'}, "完成：%d 个位置 -> %dx%d 贴图，输出到 %s"
                    % (r["count"], r["W"], r["H"], r["dir"]))
        return {'FINISHED'}


class CREATIVELAB_OT_use_active(Operator):
    bl_idname = "creativelab.use_active_object"
    bl_label = "取当前活动对象"
    bl_description = "把当前活动对象设为烘焙目标，并自动填入文件名前缀"

    def execute(self, context):
        obj = context.view_layer.objects.active
        if obj is None:
            self.report({'WARNING'}, "当前没有活动对象")
            return {'CANCELLED'}
        s = context.scene.dotforge
        s.target_object = obj
        s.prefix = obj.name.replace(" ", "_") + "_posmap"
        return {'FINISHED'}


class CREATIVELAB_PT_dotforge(Panel):
    bl_label = "DotForge · 位置图烘焙器"
    bl_idname = "CREATIVELAB_PT_dotforge"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'CreativeLab'

    def draw(self, context):
        layout = self.layout
        s = context.scene.dotforge

        col = layout.column(align=True)
        col.prop(s, "target_object")
        col.operator("creativelab.use_active_object", icon='EYEDROPPER')

        box = layout.box()
        box.label(text="位置来源", icon='MESH_DATA')
        box.prop(s, "position_source", text="")
        box.prop(s, "use_eval")

        box = layout.box()
        box.label(text="输出", icon='IMAGE_DATA')
        box.prop(s, "out_dir")
        box.prop(s, "prefix")
        box.prop(s, "unit_scale")
        row = box.row(align=True)
        row.prop(s, "square_tex", toggle=True)
        row.prop(s, "do_norm_png", toggle=True)
        row = box.row(align=True)
        row.prop(s, "do_csv", toggle=True)
        row.prop(s, "do_layout", toggle=True)

        layout.separator()
        layout.operator("creativelab.bake_position_map", icon='RENDER_STILL')

        hint = layout.box()
        hint.label(text="UE 导入设置", icon='INFO')
        hint.label(text="Compression = HDR (勿用 HDR Compressed)")
        hint.label(text="sRGB = 关,  Mip = NoMipmaps")


classes = (
    DotForgeSettings,
    CREATIVELAB_OT_bake_posmap,
    CREATIVELAB_OT_use_active,
    CREATIVELAB_PT_dotforge,
)


def register():
    for c in classes:
        bpy.utils.register_class(c)
    bpy.types.Scene.dotforge = PointerProperty(type=DotForgeSettings)


def unregister():
    del bpy.types.Scene.dotforge
    for c in reversed(classes):
        bpy.utils.unregister_class(c)


if __name__ == "__main__":
    register()
