import AppKit
import WebKit

struct LocalConfiguration {
    let root: URL
    var python: URL { root.appendingPathComponent(".local/ashare/venv/bin/python") }
    let base = URL(string: "http://127.0.0.1:8765/")!
    init() throws {
        guard let path = Bundle.main.object(forInfoDictionaryKey: "OpenPikeWorkspaceRoot") as? String,
              path.hasPrefix("/"), FileManager.default.fileExists(atPath: path + "/ashare/app.py") else {
            throw LocalFailure.message("找不到股票程序。请恢复原工程位置，或重新构建本机应用。")
        }
        root = URL(fileURLWithPath: path, isDirectory: true)
        guard FileManager.default.isExecutableFile(atPath: python.path) else {
            throw LocalFailure.message("股票独立运行环境不可用。请恢复本地依赖环境后重试。")
        }
    }
    func isLocal(_ url: URL) -> Bool {
        url.scheme == "http" && url.host == "127.0.0.1" && url.port == 8765 && url.user == nil && url.password == nil
    }
}
enum LocalFailure: LocalizedError {
    case message(String)
    var errorDescription: String? { if case .message(let text) = self { return text }; return nil }
}
func verifyService(_ config: LocalConfiguration) async throws -> [String: Any] {
    let settings = URLSessionConfiguration.ephemeral
    settings.connectionProxyDictionary = [:]
    settings.timeoutIntervalForRequest = 3
    settings.timeoutIntervalForResource = 4
    let client = URLSession(configuration: settings)
    defer { client.invalidateAndCancel() }
    let (data, response) = try await client.data(from: config.base.appendingPathComponent("api/health"))
    guard let response = response as? HTTPURLResponse, response.statusCode == 200,
          let result = try JSONSerialization.jsonObject(with: data) as? [String: Any],
          result["service_name"] as? String == "OpenPike Stocks",
          result["desktop_protocol"] as? Int == 1,
          result["workspace_root"] as? String == config.root.path else {
        throw LocalFailure.message("本地端口返回的服务不属于当前股票工程。请检查后台服务，应用未替换任何程序。")
    }
    return result
}
func installService(_ config: LocalConfiguration) throws {
    let process = Process()
    process.executableURL = config.python
    process.arguments = ["-m", "ashare.service", "install"]
    process.currentDirectoryURL = config.root
    let output = Pipe(); process.standardOutput = output; process.standardError = output
    try process.run()
    let deadline = Date().addingTimeInterval(12)
    while process.isRunning && Date() < deadline { Thread.sleep(forTimeInterval: 0.1) }
    if process.isRunning { process.terminate(); throw LocalFailure.message("后台启动超时。请查看运行记录，再重试。") }
    process.waitUntilExit()
    let text = String(data: output.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8) ?? ""
    if process.terminationStatus != 0 {
        let known = ["端口8765已被占用", "已有同名服务不属于本工程", "股票独立依赖环境尚未安装"]
        let reason = known.first(where: { text.contains($0) }) ?? "股票后台未能启动"
        throw LocalFailure.message(reason + "。请查看运行记录后重试。")
    }
}

@MainActor final class AppDelegate: NSObject, NSApplicationDelegate, WKNavigationDelegate, WKUIDelegate {
    var window: NSWindow!
    var web: WKWebView!
    var startup: NSStackView!
    var detail: NSTextField!
    var spinner: NSProgressIndicator!
    var retry: NSButton!
    var config: LocalConfiguration?
    var loading = false
    var children: [NSWindow] = []
    var startupTask: Task<Void, Never>?

    func record(_ message: String) {
        guard let root = Bundle.main.object(forInfoDictionaryKey: "OpenPikeWorkspaceRoot") as? String else { return }
        let url = URL(fileURLWithPath: root).appendingPathComponent(".local/ashare/desktop.log")
        let line = "\(ISO8601DateFormatter().string(from: Date())) \(message)\n"
        if let data = line.data(using: .utf8) {
            if let handle = try? FileHandle(forWritingTo: url) { defer { try? handle.close() }; _ = try? handle.seekToEnd(); try? handle.write(contentsOf: data) }
            else { try? data.write(to: url) }
        }
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.appearance = NSAppearance(named: .darkAqua)
        NSApp.applicationIconImage = NSImage(named: "AppIcon") ?? NSImage(contentsOf: Bundle.main.url(forResource: "OpenPikeStocks", withExtension: "icns")!)
        makeMenu()
        let screen = NSScreen.main?.visibleFrame ?? NSRect(x: 0, y: 0, width: 1440, height: 1000)
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: min(1440, screen.width * 0.94), height: min(940, screen.height * 0.90)), styleMask: [.titled, .closable, .miniaturizable, .resizable], backing: .buffered, defer: false)
        window.title = "OpenPike Stocks"
        window.collectionBehavior = [.moveToActiveSpace, .fullScreenPrimary]
        window.minSize = NSSize(width: 900, height: 640)
        window.backgroundColor = NSColor(calibratedRed: 0.043, green: 0.055, blue: 0.067, alpha: 1)
        window.setFrameAutosaveName("OpenPikeStocks.MainWindow")
        window.center()
        let content = window.contentView!
        web = WKWebView(frame: .zero, configuration: WKWebViewConfiguration())
        web.navigationDelegate = self; web.uiDelegate = self
        web.setValue(false, forKey: "drawsBackground")
        web.isHidden = true; web.translatesAutoresizingMaskIntoConstraints = false
        content.addSubview(web)
        NSLayoutConstraint.activate([web.leadingAnchor.constraint(equalTo: content.leadingAnchor), web.trailingAnchor.constraint(equalTo: content.trailingAnchor), web.topAnchor.constraint(equalTo: content.topAnchor), web.bottomAnchor.constraint(equalTo: content.bottomAnchor)])
        let logo = NSImageView(); logo.image = NSImage(contentsOf: Bundle.main.url(forResource: "brand", withExtension: "png")!)
        logo.imageScaling = .scaleProportionallyUpOrDown
        logo.translatesAutoresizingMaskIntoConstraints = false
        logo.widthAnchor.constraint(equalToConstant: 80).isActive = true; logo.heightAnchor.constraint(equalToConstant: 80).isActive = true
        let title = NSTextField(labelWithString: "OpenPike Stocks")
        title.font = .systemFont(ofSize: 28, weight: .semibold)
        detail = NSTextField(wrappingLabelWithString: "正在连接本机行情与策略档案…")
        detail.alignment = .center; detail.textColor = .secondaryLabelColor
        detail.preferredMaxLayoutWidth = 480
        spinner = NSProgressIndicator(); spinner.style = .spinning; spinner.controlSize = .regular
        retry = NSButton(title: "重试连接", target: self, action: #selector(connect))
        retry.isHidden = true
        let logs = NSButton(title: "查看运行记录", target: self, action: #selector(openLogs))
        startup = NSStackView(views: [logo, title, detail, spinner, retry, logs]); startup.orientation = .vertical; startup.spacing = 18; startup.alignment = .centerX
        startup.translatesAutoresizingMaskIntoConstraints = false; content.addSubview(startup)
        NSLayoutConstraint.activate([startup.centerXAnchor.constraint(equalTo: content.centerXAnchor), startup.centerYAnchor.constraint(equalTo: content.centerYAnchor), startup.widthAnchor.constraint(lessThanOrEqualTo: content.widthAnchor, multiplier: 0.85)])
        window.makeKeyAndOrderFront(nil); NSApp.activate(ignoringOtherApps: true)
        record("启动本机显示端；窗口 visible=\(window.isVisible)；frame=\(window.frame)")
        connect()
    }
    @objc func connect() {
        guard !loading else { return }
        loading = true; retry.isHidden = true; startup.isHidden = false; spinner.isHidden = false; spinner.startAnimation(nil)
        detail.stringValue = "正在连接本机行情与策略档案…"
        startupTask = Task { [weak self] in
            guard let self else { return }
            do {
                let cfg = try LocalConfiguration(); config = cfg
                do { _ = try await verifyService(cfg) }
                catch let error as LocalFailure { throw error }
                catch {
                    detail.stringValue = "正在启动独立股票后台…"
                    try await Task.detached { try installService(cfg) }.value
                    var connected = false
                    for _ in 0..<30 {
                        if Task.isCancelled { return }
                        do { _ = try await verifyService(cfg); connected = true; break }
                        catch let error as LocalFailure { throw error }
                        catch { try await Task.sleep(nanoseconds: 1_000_000_000) }
                    }
                    guard connected else { throw LocalFailure.message("股票后台启动后仍无法连接。请查看运行记录，再重试。") }
                }
                web.load(URLRequest(url: cfg.base, cachePolicy: .reloadIgnoringLocalCacheData))
            } catch { showFailure(error.localizedDescription) }
            loading = false
        }
    }
    func showFailure(_ text: String) {
        record("连接失败：\(text)")
        web.isHidden = true; startup.isHidden = false; spinner.stopAnimation(nil); spinner.isHidden = true
        detail.stringValue = text; retry.isHidden = false
    }
    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        if webView === web {
            startup.isHidden = true; web.isHidden = false; spinner.stopAnimation(nil)
            window.makeKeyAndOrderFront(nil); NSApp.activate(ignoringOtherApps: true)
            record("本机看板加载完成；窗口 visible=\(window.isVisible)；地址=\(webView.url?.absoluteString ?? "")")
        }
    }
    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
        if webView === web && (error as NSError).code != NSURLErrorCancelled { showFailure("看板暂时无法加载。请重试连接，或查看运行记录。") }
    }
    func webViewWebContentProcessDidTerminate(_ webView: WKWebView) {
        if webView === web { showFailure("看板显示进程已退出。请重试连接。") }
    }
    func webView(_ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction, decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        guard let url = navigationAction.request.url else { decisionHandler(.cancel); return }
        if config?.isLocal(url) == true { decisionHandler(.allow); return }
        if navigationAction.navigationType == .linkActivated && ["https", "http"].contains(url.scheme ?? "") { NSWorkspace.shared.open(url) }
        decisionHandler(.cancel)
    }
    func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration, for navigationAction: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
        guard let url = navigationAction.request.url else { return nil }
        guard config?.isLocal(url) == true else {
            if navigationAction.navigationType == .linkActivated && ["http", "https"].contains(url.scheme ?? "") { NSWorkspace.shared.open(url) }
            return nil
        }
        let child = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 960, height: 720), styleMask: [.titled, .closable, .resizable], backing: .buffered, defer: false)
        child.title = "OpenPike Stocks · 策略与证据"
        child.isReleasedWhenClosed = false
        let view = WKWebView(frame: child.contentView!.bounds, configuration: configuration)
        view.autoresizingMask = [.width, .height]; view.navigationDelegate = self; view.uiDelegate = self
        child.contentView?.addSubview(view); child.center(); children.append(child); child.makeKeyAndOrderFront(nil)
        return view
    }
    func webViewDidClose(_ webView: WKWebView) { children.first(where: { $0.contentView?.subviews.contains(webView) == true })?.close() }
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        window.makeKeyAndOrderFront(nil)
        if web.isHidden && !loading { connect() }
        return true
    }
    func applicationWillTerminate(_ notification: Notification) { startupTask?.cancel() }
    @objc func refreshView() { if web.isHidden { connect() } else { web.reload() } }
    @objc func openArchive() { openFolder(".local/ashare/strategy_archive") }
    @objc func openLogs() { openFolder(".local/ashare") }
    func openFolder(_ path: String) {
        if config == nil { config = try? LocalConfiguration() }
        if let root = config?.root { NSWorkspace.shared.open(root.appendingPathComponent(path)) }
    }
    func makeMenu() {
        let bar = NSMenu(); NSApp.mainMenu = bar
        let appMenu = NSMenu(title: "OpenPike Stocks"); let appItem = NSMenuItem(); appItem.submenu = appMenu; bar.addItem(appItem)
        appMenu.addItem(withTitle: "关于 OpenPike Stocks", action: #selector(NSApplication.orderFrontStandardAboutPanel(_:)), keyEquivalent: "")
        appMenu.addItem(.separator()); appMenu.addItem(withTitle: "隐藏 OpenPike Stocks", action: #selector(NSApplication.hide(_:)), keyEquivalent: "h")
        appMenu.addItem(withTitle: "退出 OpenPike Stocks", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        let edit = NSMenu(title: "编辑"); let ei = NSMenuItem(title: "编辑", action: nil, keyEquivalent: ""); ei.submenu = edit; bar.addItem(ei)
        for (title, action, key) in [("撤销", "undo:", "z"), ("重做", "redo:", "Z"), ("剪切", "cut:", "x"), ("复制", "copy:", "c"), ("粘贴", "paste:", "v"), ("全选", "selectAll:", "a")] { edit.addItem(withTitle: title, action: Selector(action), keyEquivalent: key) }
        let view = NSMenu(title: "查看"); let vi = NSMenuItem(title: "查看", action: nil, keyEquivalent: ""); vi.submenu = view; bar.addItem(vi)
        let reload = view.addItem(withTitle: "刷新看板", action: #selector(refreshView), keyEquivalent: "r"); reload.target = self
        let archive = view.addItem(withTitle: "打开策略档案", action: #selector(openArchive), keyEquivalent: "A"); archive.target = self
        let log = view.addItem(withTitle: "打开运行记录", action: #selector(openLogs), keyEquivalent: ""); log.target = self
        let windowMenu = NSMenu(title: "窗口"); let wi = NSMenuItem(title: "窗口", action: nil, keyEquivalent: ""); wi.submenu = windowMenu; bar.addItem(wi); NSApp.windowsMenu = windowMenu
        windowMenu.addItem(withTitle: "最小化", action: #selector(NSWindow.performMiniaturize(_:)), keyEquivalent: "m")
        windowMenu.addItem(withTitle: "关闭窗口", action: #selector(NSWindow.performClose(_:)), keyEquivalent: "w")
    }
}
if CommandLine.arguments.contains("--self-test") {
    Task {
        do {
            let config = try LocalConfiguration(); let result = try await verifyService(config)
            let output: [String: Any] = ["ok": true, "application": "OpenPike Stocks", "workspace": config.root.path,
                                       "service": result["service_name"] ?? "", "desktop_protocol": result["desktop_protocol"] ?? 0]
            let data = try JSONSerialization.data(withJSONObject: output, options: [.sortedKeys])
            print(String(decoding: data, as: UTF8.self)); exit(0)
        } catch { fputs(error.localizedDescription + "\n", stderr); exit(1) }
    }
    dispatchMain()
} else {
    MainActor.assumeIsolated {
        let application = NSApplication.shared
        let delegate = AppDelegate(); application.delegate = delegate
        application.setActivationPolicy(.regular)
        withExtendedLifetime(delegate) { application.run() }
    }
}
