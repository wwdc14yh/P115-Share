# P115-Share 🚀

**P115-Share** 是一个基于 Python 和 Vue 3 开发的 115 网盘自动化转存与分享管理系统。它能够自动监听 Telegram 消息中的 115 分享链接，将其保存至指定目录，并自动生成长期有效的二次分享链接进行广播。

![License](https://img.shields.io/badge/license-MIT-blue.svg)
![Python](https://img.shields.io/badge/python-3.11+-blue.svg)
![Docker](https://img.shields.io/badge/docker-supported-green.svg)

---

## ✨ 核心功能

### 🤖 自动化机器人流程

- **自动转存**：多平台链接支持（115.com, 115cdn.com, anxia.com），自动提取并保存至网盘。
- **Telegraph 资源解析**：自动识别 Telegram 消息正文、隐藏链接及“查看资源”等内联按钮中的 telegra.ph 地址，并解析页面内容。
- **资源链接分流**：Telegraph 页面内的 115 分享链接（包括 115cdn.com）进入自动转存和长期分享流程；`ed2k://` 链接推送至已启用自动转发的 Telegram 频道。
- **消息转发**：支持保留原始消息中的文字描述、图片和标签，并自动替换其中的链接。
- **长期链接生成**：自动将转存后的文件创建为“永久有效”的分享链接。
- **频道同步**：一键将处理结果发布到指定的 Telegram 频道。

### 📊 现代化管理面板

- **实时仪表盘**：直观查看 Bot 连接状态与 115 登录状态。
- **实时日志**：通过 WebSocket 实现的高性能日志查看器，支持历史回溯，随时掌握系统动向。
- **快捷清理**：支持一键清空保存目录及清空 115 回收站。

### ⏰ 智能维护逻辑

- **定时清理**：支持通过 Cron 表达式自定义保存目录与回收站的自动清理周期。
- **任务互斥锁**：内置高效的并发控制（Mutex），确保转存分享与清理任务安全互斥，避免冲突。
- **异常容错**：自动识别并处理 115 常见的业务错误（如文件已删除、重复接收等）。

---

## 🛠 快速开始 (Docker 部署)

### 方式一：使用 Docker 直接运行 (推荐)

直接从 Docker Hub 拉取镜像运行：

```bash
docker run -d \
  -p 8000:8000 \
  -e TZ=Asia/Shanghai \
  -v $(pwd)/data:/app/data \
  --restart unless-stopped \
  listeningltg/p115-share:latest
```

> **说明**：`data` 目录用于持久化数据库文件（包括用户头像 base64 数据）

### 方式二：使用 Docker Compose

创建 `docker-compose.yml` 文件：

```yaml
version: '3.8'

services:
  p115-share:
    image: listeningltg/p115-share:latest
    container_name: p115-share
    ports:
      - "8000:8000"
    volumes:
      - ./data:/app/data
    environment:
      - TZ=Asia/Shanghai
    restart: unless-stopped
```

然后启动服务：

```bash
docker compose up -d
```

### 方式三：本地构建

如果您想从源码构建：

```bash
git clone https://github.com/ListeningLTG/P115-Share.git
cd P115-Share
docker compose up -d --build
```

### 初始化配置

- 访问管理界面：`http://localhost:8000`
- 进入 **[系统配置]** 页面，填写以下核心信息：
  - **115 Cookie**：您的 115 账号登录凭证。
  - **TG Bot Token**：从 [@BotFather](https://t.me/BotFather) 获取。
  - **TG 用户 ID**：您的 Telegram ID（点击“测试机器人”获取）。
  - **白名单配置**：允许触发机器人的聊天 ID（用英文逗号分隔）。
  - **TG 频道 ID**：用于广播分享的频道 ID（**注意：Bot 必须被设为该频道的管理员才能发送消息**）。
  - **频道自动转发**：在频道配置中启用自动转发，机器人处理的分享结果及 Telegraph 页面提取的 `ed2k://` 链接才会自动推送到该频道。
  - **代理配置**（可选）：如需代理访问，可在"代理配置"面板中填写。
- 点击"保存配置"后，系统将持续运行。

---

## 🛠 开发与运行

如果您希望在本地进行开发或手动运行，请参考以下步骤。

### 🐍 后端开发 (Backend)

后端使用 **FastAPI** 框架。

1. **环境准备**：
   - Python 3.11+
   - 建议创建虚拟环境：

     ```bash
     cd backend
     python -m venv venv
     # Windows
     .\venv\Scripts\activate
     # Linux/macOS
     source venv/bin/activate
     ```

2. **安装依赖**：

   ```bash
   pip install -r requirements.txt
   # 安装核心组件
   pip install p115client
   ```

3. **运行服务**：

   ```bash
   # 在 backend 目录下运行
   python -m app.main
   ```

   *默认 API 端口为 8000。*

### 🎨 前端开发 (Frontend)

前端使用 **Vue 3 + Vite + Ant Design Vue**。

1. **环境准备**：
   - Node.js 20+
   - npm 或 pnpm

2. **安装依赖**：

   ```bash
   cd frontend
   npm install
   ```

3. **本地开发运行**：

   ```bash
   npm run dev
   ```

   *前端开发服务器默认运行在 `http://localhost:5173`。Vite 已配置代理，会自动将 `/api` 请求转发到 `localhost:8000`。*

4. **构建生产环境代码**：

   ```bash
   npm run build
   ```

   *构建产物将存放在 `dist` 目录。在 Docker 部署中，这些文件会被复制到后端的 `static` 目录。*

### 🔗 前后端联调

- **本地开发**：同时启动后端 (`python -m app.main`) 和前端 (`npm run dev`)。访问 `http://localhost:5173` 即可进行开发。
- **静态部署**：前端构建后的 `dist` 文件夹内容放置在 `backend/static` 下，访问后端端口即可直接查看完整应用。

---

## 📝 环境变量与配置

| 配置项 | 说明 | 默认值 |
| :--- | :--- | :--- |
| `P115_SAVE_DIR` | 转存文件存放的网盘路径 | `/分享保存` |
| `P115_CLEANUP_DIR_CRON` | 清理保存目录的周期 | `*/30 * * * *` |
| `P115_CLEANUP_TRASH_CRON` | 清理回收站的周期 | `0 */2 * * *` |
| `P115_RECYCLE_PASSWORD` | 115 回收站安全密码 | `空` |

---

## � 持续集成与 Docker Hub

项目已内置 GitHub Actions 自动化工作流。您可以轻松地将镜像发布到 Docker Hub：

1. **获取 Token**：在 [Docker Hub](https://hub.docker.com/) 的 `Account Settings -> Security` 中创建一个 `Access Token`。
2. **配置 Secrets**：在 GitHub 仓库的 `Settings -> Secrets and variables -> Actions` 中添加：
   - `DOCKERHUB_USERNAME`: 您的 Docker Hub 用户名。
   - `DOCKERHUB_TOKEN`: 刚才生成的 Access Token。
3. **触发构建**：每当您向 `main` 分支提交代码或推送形如 `v*` 的 Tag 时，GitHub 会自动构建并推送镜像到 `您的用户名/p115-share`。

---

## �📂 项目结构

```text
├── backend/            # FastAPI 后端服务
├── frontend/           # Vue 3 前端源码
├── Helper/             # p115client 核心组件
├── Dockerfile          # 多阶段构建文件
└── docker-compose.yml  # 容器编排文件
```

---

## ⚠️ 免责声明

本程序仅供技术研究和学习，禁止用于任何非法用途。开发者不对使用本程序造成的任何数据损失或账号问题负责。请遵守相关法律法规及网盘平台使用协议。

---

## 🤝 贡献

欢迎提交 Issue 或 Pull Request！

**Created by Listening © 2026**
