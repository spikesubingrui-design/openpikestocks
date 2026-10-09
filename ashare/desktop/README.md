# OpenPike Stocks 本机应用

桌面和「应用程序」中双击 OpenPike Stocks，显示独立 Mac 窗口与 Dock 图标。应用首次连接时核实本机服务身份；股票后台未启动则启动已有的独立 LaunchAgent。端口或同名服务属于其他工程时停止启动并显示原因。

首页上方固定显示「当前最推荐买点」「持仓卖出提醒」。所有条件通过后只显示合格第一名及入场卖一、原失效价、第一压力、净盈亏比。无合格买点显示等待和观察第一名的阻断原因；评分不等于胜率。卖出依据实际登记的每批持仓，仅提示当前条件，不复用历史退出事件。显示 T+1、休市、过期报价和无法成交限制；纸面账户独立。

菜单「查看」可刷新（⌘R）、打开策略档案（⇧⌘A）和运行记录。实际成交仍需在券商进行，然后手工登记；应用不会产生证券委托。

关闭应用窗口会退出显示端，股票后台继续监测；开机且用户登录时后台自动运行。股票服务、数据及运行环境仍位于原工程 `.local/ashare/`，期货程序独立。本机版本不是供其他电脑独立安装的离线包；移动或删除原工程会使应用无法启动。

源码 `OpenPikeStocks.swift` 使用 macOS AppKit / WKWebView。`build_app.py --install` 用本机 Apple Swift 工具链构建 arm64、macOS 14+ 应用并安装到 `/Applications/OpenPike Stocks.app`。本地自签名用于此电脑，未作 App Store 分发或公证。

窗口仅内嵌当前本机地址；点击公开研究来源时交由默认浏览器打开。网络例外限于 [Apple 本地网络设置](https://developer.apple.com/documentation/bundleresources/information-property-list/nsapptransportsecurity/nsallowslocalnetworking)。不加载外部页面作为交易看板。
