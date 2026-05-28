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
autossh -M 0 -N -p 60022 -i /data/ssh/tunnel_key -R 0.0.0.0:3000:10.1.1.8:3000 root@hrlu.cn
```

外网 SSH 主机独立管理，转发规则只需要选择对应外网主机。一个转发规则可以包含多个端口映射，例如 `3000,10080,2222:22` 会生成多个 `-R` 参数，并作为同一个 autossh 进程一起启停。

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

外网主机字段：

- `外网 SSH 主机`：运行 sshd 的公网服务器，例如 `hrlu.cn`
- `外网 SSH 端口`：公网服务器 sshd 端口，例如 `60022`
- `外网监听 IP`：通常是 `0.0.0.0`
- `SSH 私钥路径`：容器内运行时路径，compose 示例为 `/data/ssh/tunnel_key`

转发规则字段：

- `外网主机`：选择已创建的外网 SSH 主机
- `内网目标主机`：本地网络里的服务 IP，例如 `10.1.1.8`
- `端口映射`：支持每行、逗号或分号分隔；`3000` 表示 `3000:3000`，`2222:22` 表示外网 `2222` 到内网 `22`

## SSH 私钥权限

OpenSSH 会拒绝权限过宽的私钥，例如常见报错：

```text
WARNING: UNPROTECTED PRIVATE KEY FILE!
Permissions 0644 for 'id_rsa' are too open.
```

compose 中不要把私钥直接挂到 `~/.ssh/id_rsa` 作为容器内路径；容器内路径应使用绝对路径。也不要直接 bind 单个私钥文件，否则宿主机文件不存在时 Docker 可能创建同名目录。当前配置会把宿主机 `${HOME}/.ssh` 目录只读挂载到 `/host_ssh`，容器启动时自动查找 `id_ed25519`、`id_rsa`、`id_ecdsa`、`id_dsa`，复制找到的私钥到 `/data/ssh/tunnel_key`，并自动设置：

```text
chmod 700 /data/ssh
chmod 600 /data/ssh/tunnel_key
```

如果要明确指定私钥文件名，可以在 compose 环境变量中设置：

```yaml
- SSH_KEY_NAME=id_ed25519
```

程序启动时也会把数据库里已保存的旧默认路径自动改为当前 `SSH_KEY_RUNTIME_PATH`，避免旧规则继续使用权限不正确的挂载路径。

如果宿主机本身也要直接使用该私钥，建议同时执行：

```bash
chmod 700 ~/.ssh
chmod 600 ~/.ssh/id_rsa
```

## 首次导入

当前版本将外网主机和转发规则拆分为独立表。旧版数据库不会迁移历史数据；容器启动时会重建为新结构。

当 `/data/tunnels.db` 为空时，程序可读取 `BOOTSTRAP_HOSTS` 和 `BOOTSTRAP_TUNNELS` 导入初始规则。数据库创建后，后续修改以 Web 页面为准。

示例：

```yaml
environment:
  - BOOTSTRAP_HOSTS=[{"name":"hrlu-cn","remote_user":"root","remote_host":"hrlu.cn","ssh_port":60022,"remote_bind_ip":"0.0.0.0","ssh_key_path":"/data/ssh/tunnel_key","enabled":1}]
  - BOOTSTRAP_TUNNELS=[{"name":"dpannel-linux","host_name":"hrlu-cn","target_host":"10.1.1.8","port_mappings":"3000,10080","enabled":1},{"name":"windows-10-rdp","host_name":"hrlu-cn","target_host":"10.1.1.12","port_mappings":"3389","enabled":1}]
```

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
