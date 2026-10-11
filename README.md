# CreativeLab · DotForge

**PositionMap Baker** —— 把网格元素的位置烘焙成一张 **EXR 位置贴图**，供 Unreal Engine Niagara 当作粒子位置贴图使用。

每个纹素 = 一个粒子的位置（单位 cm）。常用于 LED 点阵屏、实例化几何、静态粒子布局等场景。

---

## Features / 功能

- 三种位置来源：
  - **Mesh Island Center（网格孤岛中心）** —— 每个独立网格块算一个位置（如每颗 LED 灯珠）
  - **Face Center（面中心）** —— 每个 Polygon 中心
  - **Vertex（顶点）** —— 每个顶点
- 可选使用**求值后的网格**（应用几何节点 / 修改器结果）
- **32-bit float 无损 EXR**（`compression = NONE`，纹素 RGB = 世界坐标，A = 1）
- 附带归一化 PNG、CSV、布局校验图、info.json
- 自动排布为接近方形的贴图（`W = ceil(sqrt(N))`，`H = ceil(N / W)`）

## Installation / 安装

需要 Blender 4.0 及以上（在 Blender 5.1 上开发并测试）。

1. 下载 `creative_lab_dotforge.py`
2. Blender → `Edit > Preferences > Add-ons > Install from Disk...`
3. 选择该 `.py` 文件，勾选启用 **CreativeLab · DotForge**

## Usage / 使用

1. 3D 视图右侧按 `N` 打开侧边栏，切到 **CreativeLab** 标签页
2. 选择目标对象（点「取当前活动对象」可自动填入当前选中对象并生成文件名前缀）
3. 选择位置来源、输出目录、文件名前缀
4. 点「烘焙位置图 (EXR)」

## Output / 输出

设位置数量为 N，`W = ceil(sqrt(N))`，`H = ceil(N / W)`：

| 文件 | 说明 |
| --- | --- |
| `<prefix>_WxH.exr` | 位置贴图，纹素 RGB = `(x, y, z)` cm，A = 1 |
| `<prefix>_WxH_norm.png` | 归一化版本（0..1）：`pos = bbox_min + rgb * (bbox_max - bbox_min)` |
| `<prefix>_layout_check.png` | 按 x/z 平面画点，肉眼校验排布 |
| `<prefix>_positions.csv` | 逐点坐标 `x_cm,y_cm,z_cm`（行序 = 纹素索引序） |
| `<prefix>_info.json` | 尺寸、bbox、索引到 UV 的映射等元数据 |

## Texture layout / 贴图布局约定

```
i    = 纹素线性索引
col  = i % W          row = i // W
u    = (col + 0.5) / W
v    = (row + 0.5) / H        # UE 的 V = 0 在图片左上角
```

图片顶行 = 索引 `0 .. W-1`。

## Unreal Engine import / UE 导入设置

位置贴图必须**无损**导入，否则解出的坐标会整体偏移：

| 设置 | 值 |
| --- | --- |
| Compression Settings | **HDR**（未压缩 16F） |
| sRGB | **关闭** |
| Mip Gen Settings | **NoMipmaps** |

> ⚠️ 不要选 `HDR Compressed`（BC6H）。它按块共享指数的有损量化会让解码坐标偏移约 2~3 cm，
> 并且采样值不再落在任何真实纹素上（表现为"怎么调都对不齐"）。

**Niagara 端**：粒子的 `Index` 对应纹素线性索引，`Position = texel.rgb`。

- GPU 模拟：用 `LoadTexture2D(col, row, 0, texel)` 整数纹素直读（无过滤、无 mip 混色）
- CPU 模拟：用 `SampleTexture2D((float2(col, row) + 0.5) / dims, 0, texel)`，显式加半纹素偏移避免双线性混色

粒子数需与使用的纹素数**严格对应**（N 个粒子对 N 个有效纹素，不足的补位纹素不要派粒子）。

## Notes / 说明

- 位置由对象 `matrix_world` 变换到世界坐标，再乘以 `unit_scale`（默认 `100`，即 UE 的 cm）。
- 若你的平面不在 `y = 0`（例如整块屏位于某个世界 y 偏移上），解出的 `Position.y` 会带上该偏移，
  需要的话在 Niagara 里减掉或在 Blender 里先归零。

## License

MIT License —— 见 [LICENSE](LICENSE)。
