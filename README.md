# 项目里的东西
- 由deepseek harness协助开发的脚本，可以快速拉取prts.wiki(https://prts.wiki/)的干员信息中的模型，目前仅支持提取默认状态的png格式。
- 后续会增加提取其它模型
# 声明
- 本项目所有静态资源，版权均属于Arknights/上海鹰角网络科技有限公司，仅用于学习和交流，侵删。


# PRTS 干员模型 PNG 批量导出（以下是AI写的）

复刻 [prts.wiki](https://prts.wiki/w/%E6%A1%83%E9%87%91%E5%A8%98#%E5%B9%B2%E5%91%98%E6%A8%A1%E5%9E%8B)
干员页面「干员模型」小节里那个 **导出 PNG** 按钮的行为，去掉浏览器交互，一次把全部干员的
**默认状态**模型图导出来。

- 纯 Python 标准库，**不需要 `pip install`**，也不需要 Node
- 用你机器上已有的 **Chrome / Edge** 跑无头渲染，出图与网页上点按钮得到的完全一致（同一套官方渲染代码）
- 实测全量 463 个干员：**102 秒 / 6 并发**（1000×1000，透明底，0 失败）

## 目录里已经有什么

```
prts-model-downloader/
├── prts_models.py     ← 工具本体（单文件）
├── README.md
├── .prts_cache/       ← 干员清单 / 模型元数据 / 查看器代码镜像（约 1.7 MB，可随时删）
└── prts_models/       ← 已经导好的成品：463 个干员 × 默认状态，1000×1000 透明底 PNG（约 85 MB）
```

想换参数重新导（比如更高分辨率），删掉 `prts_models/` 里对应文件、或换个 `--out` 目录再跑即可；
已存在且分辨率一致的文件会自动跳过。

## 快速开始

```bash
cd prts-model-downloader

# 全部干员（默认：透明底 / 1000x1000 / 文件名带 charId）
python prts_models.py --all

# 只导几个
python prts_models.py --ids char_151_myrtle,char_002_amiya

# 先看看会导出什么，不下载
python prts_models.py --all --dry-run
python prts_models.py --list-only        # 列出干员名 + charId
```

输出默认落在 `./prts_models/`，文件名形如：

```
char_151_myrtle__桃金娘-默认-正面-Idle-f0.png
char_002_amiya__阿米娅-默认-正面-Idle-f0.png
```

（`干员名-皮肤-视角-动作-帧号`，其中皮肤/视角/动作沿用网页上的命名，所以一眼能看出是什么状态。）

## 常用参数

| 参数 | 说明 |
| --- | --- |
| `--all` / `--ids a,b` / `--ids-file f.txt` | 导出范围；`--ids-file` 每行一个 charId，`#` 开头为注释 |
| `--out DIR` | 输出目录，默认 `prts_models` |
| `--workers N` | 并发数，默认 4（网络差或想更轻量就调成 2） |
| `--bg transparent\|light\|dark\|default` | 背景，默认 `transparent` 透明底；`light` 是网页默认的浅灰 `#f2f2f2` |
| `--size 1000\|2000\|4000` | 导出边长，默认 1000（与网页一致）。网页的 PNG 按钮固定 1000×1000，本工具通过放大渲染分辨率给出更高清版本，取景完全不变 |
| `--name id\|wiki` | 文件名策略，默认 `id` 带 charId 前缀（不同形态的阿米娅等同名干员不会互相覆盖）；`wiki` 只用网页原名 |
| `--limit N` | 只处理前 N 个，试跑用 |
| `--chrome PATH` | 手动指定浏览器可执行文件 |
| `--list-only` / `--dry-run` | 只列清单 / 只统计待导出数量 |
| `--refresh` | 忽略缓存，重新拉干员清单、元数据与查看器代码 |
| `--keep-profile` | 保留临时浏览器 profile，排查问题用 |

## 它到底做了什么

1. **干员清单**：从 `prts.wiki` 的 `Category:干员` 分类取页面名，再解析每页「干员模型」小节里的
   `#spine-root data-id`，得到 `charId`（结果缓存在 `.prts_cache/operators.json`）。
2. **模型元数据**：`https://torappu.prts.wiki/assets/char_spine/<charId>/meta.json`，
   里面记录每套皮肤的 `正面 / 背面 / 基建` 三份模型（`.skel` + `.atlas` + 贴图 `.png`）。
3. **渲染**：网页上的查看器是 `static.prts.wiki` 上的一个小部件（SpineViewer，Pixi + Spine 3.8）。
   它只对 `prts.wiki` 这一个来源发 CORS 头，所以本工具把小部件**镜像到本地**（缓存在
   `.prts_cache/widgets/`），用一个本地 HTTP 服务喂给无头浏览器；模型资源本身是 CORS 全开的，
   浏览器直接去 `torappu.prts.wiki` 取。
4. **逐个导出**：每个模型开一个同源 iframe 加载查看器（`srcdoc`），点一次「载入模型」，
   部件就会自动选中 **默认皮肤 + 正面 + 待机** 并完成取景；然后把时间轴钉在第 0 帧，
   给 `URL.createObjectURL` / 锚点 `click` 打补丁，**截获**导出的 PNG Blob 回传落盘 ——
   不会往你的下载目录里丢文件。
5. 已经导出过（且分辨率一致）的会跳过，中断后重跑可以续传。

## 说明与边界

- **只导默认状态**：即打开网页后查看器默认选中的「默认皮肤 / 正面 / 待机 / 第 0 帧」。
  同一干员的其他皮肤（`meta.json` 里的 `淡金庆典`、`夏卉 FA062` …）和 `背面 / 基建` 视角没有批量导出，
  需要的话可以在此基础上扩展（皮肤和视角是查看器里的两个 Naive UI 下拉，脚本里点选即可）。
- **第 0 帧**：导出文件名里的 `-f0` 就是这个含义。如果你想要"随手一点"的那种随机帧，
  可以把驱动脚本里钉帧的两行去掉。
- 依赖本机能跑 Chrome/Edge；Windows / macOS / Linux 都会自动找常见安装路径。
- 请自行控制并发与频率，别把人家 wiki 和静态资源站打疼了。
- 资源版权归鹰角网络 / PRTS 各贡献者所有，导出的图请按原站要求使用。

## 常见问题

**报错「查看器部件未挂载」**：多半是拉取小部件或模型资源失败（网络/代理）。
用 `--refresh` 重试，或加 `--keep-profile` 后手动打开脚本打印的本地地址看控制台报错。

**浏览器起不来**：加 `--chrome "C:\Program Files\Google\Chrome\Application\chrome.exe"` 明确指定路径。

**想导出网页默认的浅灰底**：`--bg light`。
