# 端口转发管理站

这是一个 Python Web + autossh 的反向端口转发管理站，用来把本地内网服务暴露到外网 SSH 服务器上。

你的原始配置等价于多条 SSH 远程端口转发：

```text
hrlu.cn:3000  -> 10.1.1.8:3000
hrlu.cn:10080 -> 10.1.1.8:10080
hrlu.cn:3389  -> 10.1.1.12:3389
```

实现方式是容器内执行：

```bash
autossh -M 0 -N -p 60022 -i /data/ssh/id_rsa -R 0.0.0.0:3000:10.1.1.8:3000 root@hrlu.cn
```

每个外网监听端口是一条独立规则，可以在 Web 页面中启动、停止、重启、编辑和查看日志。

## 部署

先确认外网 SSH 服务器 `/etc/ssh/sshd_config` 允许远程绑定到公网地址：

```text
GatewayPorts yes
AllowTcpForwarding yes
```

修改 `docker-compose.yml` 中的 `ADMIN_PASSWORD`、`SECRET_KEY`、SSH 主机和端口，然后启动：

```bash
docker compose up -d --build
```

打开：

```text
http://服务器IP:8080
```

默认用户名来自 `ADMIN_USERNAME`，示例为 `admin`。

## 配置说明

主要字段：

- `外网 SSH 主机`：运行 sshd 的公网服务器，例如 `hrlu.cn`
- `外网 SSH 端口`：公网服务器 sshd 端口，例如 `60022`
- `外网监听 IP`：通常是 `0.0.0.0`
- `外网监听端口`：公网服务器对外开放的端口，例如 `3000`
- `内网目标主机`：本地网络里的服务 IP，例如 `10.1.1.8`
- `内网目标端口`：本地服务端口，例如 `3000`
- `SSH 私钥路径`：容器内运行时路径，compose 示例为 `/data/ssh/id_rsa`

## SSH 私钥权限

OpenSSH 会拒绝权限过宽的私钥，例如常见报错：

```text
WARNING: UNPROTECTED PRIVATE KEY FILE!
Permissions 0644 for 'id_rsa' are too open.
```

compose 中不要把私钥直接挂到 `~/.ssh/id_rsa` 作为容器内路径；容器内路径应使用绝对路径。当前配置会把宿主机 `${HOME}/.ssh/id_rsa` 只读挂载到 `/run/secrets/tunnel_id_rsa`，容器启动时复制到 `/data/ssh/id_rsa`，并自动设置：

```text
chmod 700 /data/ssh
chmod 600 /data/ssh/id_rsa
```

程序启动时也会把数据库里已保存的旧默认路径 `~/.ssh/id_rsa`、`/id_rsa`、`/run/secrets/tunnel_id_rsa` 自动改为当前 `SSH_KEY_RUNTIME_PATH`，避免旧规则继续使用权限不正确的挂载路径。

如果宿主机本身也要直接使用该私钥，建议同时执行：

```bash
chmod 700 ~/.ssh
chmod 600 ~/.ssh/id_rsa
```

## 首次导入

当 `/data/tunnels.db` 为空时，程序会读取 `BOOTSTRAP_TUNNELS` 导入初始规则。数据库创建后，后续修改以 Web 页面为准。

也兼容单服务旧环境变量：

```yaml
environment:
  - SSH_REMOTE_USER=root
  - SSH_REMOTE_HOST=hrlu.cn
  - SSH_REMOTE_PORT=60022
  - SSH_BIND_IP=0.0.0.0
  - SSH_TUNNEL_PORT=3000,10080
  - SSH_TARGET_HOST=10.1.1.8
  - SSH_TARGET_PORT=3000,10080
```

## pip 清华源

`Dockerfile` 已配置：

```dockerfile
PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
pip install -i https://pypi.tuna.tsinghua.edu.cn/simple
```
