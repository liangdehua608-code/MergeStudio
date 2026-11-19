# 视频拼接工坊 · MergeStudio

把一整个文件夹的网课视频按顺序拼成一条长视频，顺带解决时长错乱、音视频不同步、
台标遮挡等问题。所有处理都在本机完成，文件不上传。

> v6 把原来的单文件脚本重构成可维护的 Python 包，并新增「画面裁剪」与「提取音频」。

![任务队列](docs/screenshots/queue.png)

## 功能

**合并**
- 按文件夹批量合并，支持拖拽调整顺序、包含子文件夹
- 合并前预检：红/黄/绿健康度，标出时间基/采样率异类、无音频、Edit List 等风险
- 三种模式：自动（参数一致则无损直拷，少数派单独转码）、全部直拷、全部转码
- 统一音频采样率与视频 time_base，避免拼完时长被拉伸
- 批量裁剪片头片尾、写入章节标记、合并后自检（时长/同步/帧数/章节）

**画面裁剪（v6 新增）**
- 去掉画面顶部的台标、四周黑边，直接在预览帧上框出保留区域
- 预设「去顶部 60 / 80 px」「上下各 40 px」，也可手填四边像素
- 自动检测黑边（cropdetect），台标建议用预设
- 可选「裁完缩放回原分辨率」，默认保留裁剪后的尺寸（不拉伸）

**提取音频（v6 新增）**
- 批量把视频音轨导出为 m4a（直接复制，最快、无损）或 mp3（libmp3lame）
- 自动跳过没有音轨的文件，并在任务报告里列出

**任务队列**
- 单 worker 串行执行，避免多任务抢磁盘
- 暂停/继续队列、调整优先级、取消、重试、移除、清理已完成
- 每个任务独立的进度、日志、自检报告；历史落盘，重启后仍可查
- 批量建任务：父目录下每个子文件夹各建一个任务

## 截图

| 新建任务 · 预检 + 画面裁剪 | 提取音频 |
| --- | --- |
| ![裁剪](docs/screenshots/crop.png) | ![音频](docs/screenshots/audio.png) |

## 环境要求

- Python 3.9+
- ffmpeg / ffprobe（需自行安装，建议 5.0+；需要 libx264，字幕烧录另需 libass）
  - Windows 可用 [gyan.dev](https://www.gyan.dev/ffmpeg/builds/) 的 full build
  - 也可以在网页「设置」里填 ffmpeg.exe / bin 目录，留空则自动从 PATH 查找

## 快速开始

```bash
git clone <your-repo-url> MergeStudio
cd MergeStudio
python -m mergestudio            # 默认 http://127.0.0.1:8760/ 并自动打开浏览器
```

Windows 也可以直接双击 `启动.bat`（停止用 `停止.bat`）。

常用参数：

```bash
python -m mergestudio --port 9000     # 指定端口
python -m mergestudio --no-browser    # 不自动开浏览器
python -m mergestudio --stop          # 停止正在运行的实例
```

重复启动会自动复用已有实例并打开浏览器，不会起第二个服务。

## 目录结构

```
MergeStudio/
├── mergestudio/
│   ├── __main__.py      # 命令行入口、单实例、优雅退出
│   ├── config.py        # 路径、ffmpeg 解析与配置读写
│   ├── media.py         # ffprobe 探测、扫描分组、ffmpeg 执行、抽帧、cropdetect
│   ├── task.py          # 任务对象与 JOB 代理
│   ├── queue.py         # 串行队列、历史落盘
│   ├── runtime.py       # 进程探活、端口探测、运行记录
│   ├── jobs/
│   │   ├── merge.py     # 合并 / 裁剪 / 章节 / 自检
│   │   └── audio.py     # 提取音频
│   └── web/
│       ├── __init__.py  # HTTP 服务与 JSON API
│       ├── index.html
│       └── static/      # app.js / style.css
├── tests/test_smoke.py  # 需要本机 ffmpeg 的端到端冒烟测试
├── docs/screenshots/
├── run.py               # python run.py 等价于 python -m mergestudio
├── 启动.bat / 停止.bat
└── start.sh
```

## 数据与配置

运行时数据都在项目根目录的 `.mergestudio/`（已加入 `.gitignore`）：

- `config.json`：ffmpeg / ffprobe 路径
- `history.json`：已结束任务的历史（只保留最近 40 条）
- `runtime.json`：当前实例的 PID 与端口，用于单实例判断

## HTTP API（本地）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/ping` | 实例标识 |
| GET | `/api/config` | ffmpeg 状态 |
| POST | `/api/config` | 验证并保存路径 |
| POST | `/api/scan` | 扫描文件夹并预检 |
| POST | `/api/detect-crop` | cropdetect 自动检测黑边 |
| GET | `/api/thumb?path=&t=` | 抽取一帧 JPEG 用于裁剪预览 |
| GET | `/api/queue` | 队列快照 |
| GET | `/api/task?id=` | 单任务详情（含日志） |
| POST | `/api/queue/{add,batch,cancel,retry,remove,move,clear,pause}` | 队列操作 |
| POST | `/api/shutdown` | 优雅退出（先取消任务） |

## 测试

```bash
python -m unittest tests.test_smoke -v
```

测试会用 ffmpeg 现场生成几段 2 秒的测试片，跑通「裁剪合并 → 抽帧 → 提取音频」，
不依赖任何真实素材；没有配置 ffmpeg 时会自动跳过。

## 常见问题

**双击 bat 闪一下就没了？**
脚本里的中文提示必须用 GBK 保存（本仓库的 bat 已经是 GBK + `chcp 936`）。
如果用编辑器改成 UTF-8，cmd 会把多字节字符截断，导致整段脚本解析失败。

**裁剪后为什么变慢了？**
裁剪无法流复制，必须重编码，因此启用裁剪会强制该任务「全部转码」。
如果只是去掉片头片尾，用「片头/片尾裁剪」即可保持无损。

**字幕能提取吗？**
B 站课程的视频通常没有字幕轨（字幕已经烧进画面），无法提取。
本工具预留了字幕能力的方向，但当前版本只做音频提取。

## Roadmap

- [ ] 按章节 / 时长切割
- [ ] 字幕烧录与软字幕封装
- [ ] 断点续跑（保留已完成分段）
- [ ] 分段容错：跳过失败集并生成报告
- [ ] 任务完成系统通知 / 托盘

## License

[MIT](LICENSE)
