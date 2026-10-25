# QQ Codex Agent

通过 NapCat OneBot 11 连接 QQ，使用本地 Codex runtime 和 ChatGPT 登录的个人 AI 助手，支持聊天、图片理解与生成、文件操作和代码执行。

配置允许的私聊用户及群后即可聊天；群内无须 @ 也可参与，使用指令时需 @ bot。通过 `/help` 查看指令，使用 `/stop` 停止任务。

支持 Python 独立运行，也支持 NixOS 服务部署；当前部署在树莓派，应用由 Nix 打包，可复用宿主的共享 ChatGPT 登录。

配置、行为、开发及部署文档见 [docs/](docs/README.md)。
