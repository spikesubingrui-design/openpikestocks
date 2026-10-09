# 可选远程访问

公开版本默认只监听本机。远程桥接与会话源码作为可选参考保留，示例域名为 `stocks.example.com`，并非在线体验地址。没有附带个人云服务器、部署脚本、密码、令牌或数据库。

默认安装步骤不会启动远程桥接。自行部署前需在 remote.py、remote_client.py、cloud_relay.py、remote_service.py 及静态页面中统一设置自己的域名，安装相应锁定依赖并验证 TLS、登录、注销、未登录访问、失联和恢复。云端 relay 需要独立配置 OPENPIKE_RELAY_TOKEN，与本机自动生成的 relay 凭据一致。不要直接将本机 API 端口暴露到公网。
