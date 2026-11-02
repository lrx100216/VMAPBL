VMAPBL Butler — 双击使用说明

1. 保留整个 VMAPBL 项目目录，不要只移动 .app 文件。
2. 在 Finder 中双击 VMAPBL Butler.app：
   - 首次：启动 main.py --serve，就绪后自动打开 http://127.0.0.1:8321/
   - 已运行：只打开控制台，不重复启动服务
3. 如系统请求摄像头或麦克风权限，请允许。
   若未弹出授权框，请先在 Terminal.app 中运行一次：
   .venv/bin/python tools/permission_check.py
4. 启动日志：logs/launcher.out.log、logs/butler.log
5. 本应用依赖项目内的 .venv 和模型文件，不是独立免安装包。
