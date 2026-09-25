import AppKit
import Foundation
import PDFKit
import UserNotifications

// Extraction runs in a separate, time-limited process controlled by the service.
if let index = CommandLine.arguments.firstIndex(of: "--extract-text"), CommandLine.arguments.count > index + 1 {
    let url = URL(fileURLWithPath: CommandLine.arguments[index + 1])
    let document = PDFDocument(url: url)
    let result = ["text": String((document?.page(at: 0)?.string ?? "").prefix(8000)),
                  "title": String((document?.documentAttributes?[PDFDocumentAttribute.titleAttribute] as? String ?? "").prefix(300))]
    if let data = try? JSONSerialization.data(withJSONObject: result) {
        FileHandle.standardOutput.write(data)
    }
    exit(document == nil ? 1 : 0)
}

@MainActor enum Palette {
    static let canvas = NSColor(srgbRed: 0.97, green: 0.955, blue: 0.93, alpha: 1)
    static let ink = NSColor(srgbRed: 0.19, green: 0.23, blue: 0.21, alpha: 1)
    static let muted = NSColor(srgbRed: 0.43, green: 0.47, blue: 0.43, alpha: 1)
    static let orange = NSColor(srgbRed: 0.72, green: 0.28, blue: 0.17, alpha: 1)
    static let sage = NSColor(srgbRed: 0.91, green: 0.94, blue: 0.88, alpha: 1)
}

@MainActor final class ActionButton: NSButton {
    var usesExplicitKeyLoop = false
    override var acceptsFirstResponder: Bool { isEnabled }
    override func performKeyEquivalent(with event: NSEvent) -> Bool {
        if event.keyCode == 36, window?.firstResponder === self {
            performClick(nil)
            return true
        }
        return super.performKeyEquivalent(with: event)
    }
    override func keyDown(with event: NSEvent) {
        if event.keyCode == 48 && usesExplicitKeyLoop {
            let next = event.modifierFlags.contains(.shift) ? previousKeyView : nextKeyView
            if let next {
                window?.makeFirstResponder(next)
                next.scrollToVisible(next.bounds)
            }
        } else if event.keyCode == 36 || event.keyCode == 49 { performClick(nil) }
        else { super.keyDown(with: event) }
    }
    var primary = false { didSet { needsDisplay = true } }
    private var hoverTracking: NSTrackingArea?
    private var hovering = false
    private var focused = false
    override func becomeFirstResponder() -> Bool {
        let accepted = super.becomeFirstResponder()
        focused = accepted
        updateHover()
        return accepted
    }
    override func resignFirstResponder() -> Bool {
        let accepted = super.resignFirstResponder()
        if accepted { focused = false; updateHover() }
        return accepted
    }
    @objc dynamic var hoverAmount: CGFloat = 0 {
        didSet {
            contentTintColor = Palette.muted.blended(withFraction: hoverAmount, of: Palette.orange)
            needsDisplay = true
        }
    }
    override class func defaultAnimation(forKey key: NSAnimatablePropertyKey) -> Any? {
        key == NSAnimatablePropertyKey("hoverAmount") ? CABasicAnimation() : super.defaultAnimation(forKey: key)
    }
    override func updateTrackingAreas() {
        if let hoverTracking { removeTrackingArea(hoverTracking) }
        let area = NSTrackingArea(rect: .zero, options: [.mouseEnteredAndExited, .activeInKeyWindow, .inVisibleRect], owner: self)
        addTrackingArea(area)
        hoverTracking = area
        super.updateTrackingAreas()
    }
    override func mouseEntered(with event: NSEvent) { hovering = true; updateHover() }
    override func mouseExited(with event: NSEvent) { hovering = false; updateHover() }
    private func updateHover() {
        let target: CGFloat = (hovering || focused) && isEnabled ? 1 : 0
        NSAnimationContext.runAnimationGroup { context in
            context.duration = NSWorkspace.shared.accessibilityDisplayShouldReduceMotion ? 0 : 0.14
            animator().hoverAmount = target
        }
    }
    override var isEnabled: Bool {
        didSet { if oldValue != isEnabled { updateHover(); needsDisplay = true } }
    }
    override var title: String { didSet { needsDisplay = true } }
    override func draw(_ dirtyRect: NSRect) {
        guard primary else {
            super.draw(dirtyRect)
            return
        }
        let warm = Palette.orange.blended(withFraction: 0.10 * hoverAmount, of: .black) ?? Palette.orange
        (isEnabled ? warm : Palette.orange.withAlphaComponent(0.35)).setFill()
        NSBezierPath(roundedRect: bounds.insetBy(dx: 1, dy: 1), xRadius: 10, yRadius: 10).fill()
        let text = NSAttributedString(string: title, attributes: [.font: NSFont.systemFont(ofSize: 13, weight: .semibold), .foregroundColor: NSColor.white])
        let size = text.size()
        text.draw(at: NSPoint(x: (bounds.width - size.width) / 2, y: (bounds.height - size.height) / 2))

    }
    var handler: () -> Void = {}
    convenience init(_ title: String, action: @escaping () -> Void) {
        self.init(title: title, target: nil, action: #selector(invoke))
        self.target = self
        self.handler = action
        self.bezelStyle = .rounded
        self.isBordered = false
        self.focusRingType = .none
        self.contentTintColor = Palette.muted
        self.font = .systemFont(ofSize: 12, weight: .medium)
    }
    @objc func invoke() { handler() }
}

@MainActor final class FolderPicker: NSButton {
    private var folders: [(id: String, parts: [String])] = []
    private var path: [String] = []
    private var navigationButtons: [NSButton] = []
    override var acceptsFirstResponder: Bool { isEnabled }
    private let popover = NSPopover()
    private(set) var selectedFolderID: String?
    var onChange: () -> Void = {}

    convenience init(folders: [[String: Any]], selectedID: String?) {
        self.init(frame: .zero)
        self.folders = folders.compactMap {
            guard let id = $0["id"] as? String, let label = $0["label"] as? String else { return nil }
            return (id, label.components(separatedBy: " / "))
        }
        selectedFolderID = self.folders.contains(where: { $0.id == selectedID }) ? selectedID : nil
        title = self.folders.first(where: { $0.id == selectedID })?.parts.joined(separator: " / ") ?? "Choose a folder"
        isBordered = false
        focusRingType = .none
        font = .systemFont(ofSize: 13, weight: .medium)
        cell?.lineBreakMode = .byTruncatingMiddle
        setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        target = self
        action = #selector(openFolders)
        setAccessibilityLabel("Destination folder")
        setAccessibilityHelp("Browse folders and choose a destination.")
        popover.behavior = .semitransient
        popover.animates = false
    }

    override func draw(_ dirtyRect: NSRect) {
        let text = NSAttributedString(string: title + "  ›", attributes: [
            .font: font ?? NSFont.systemFont(ofSize: 13),
            .foregroundColor: !isEnabled ? Palette.muted : (window?.firstResponder === self ? Palette.orange : Palette.ink)])
        let paragraph = NSMutableParagraphStyle()
        paragraph.lineBreakMode = .byTruncatingMiddle
        let fitted = NSMutableAttributedString(attributedString: text)
        fitted.addAttribute(.paragraphStyle, value: paragraph, range: NSRange(location: 0, length: fitted.length))
        fitted.draw(in: bounds.insetBy(dx: 4, dy: 2))

    }

    @objc private func openFolders() {
        path = []
        renderFolders()
        popover.show(relativeTo: bounds, of: self, preferredEdge: .maxY)
        focusBrowser()
    }

    private func focusBrowser() {
        let window = popover.contentViewController?.view.window
        window?.makeKey()
        window?.makeFirstResponder(navigationButtons.first)
    }

    private func renderFolders() {
        navigationButtons = []
        let controller = NSViewController()
        let root = NSStackView()
        root.orientation = .vertical
        root.alignment = .leading
        root.spacing = 12
        root.wantsLayer = true
        root.layer?.backgroundColor = Palette.canvas.cgColor
        if !path.isEmpty {
            let back = ActionButton("‹  Back") { [weak self] in
                guard let self else { return }
                self.path.removeLast()
                self.renderFolders()
            }
            root.addArrangedSubview(back)
            navigationButtons.append(back)
        }
        let heading = NSTextField(labelWithString: path.isEmpty ? "Choose a folder" : path.joined(separator: " / "))
        heading.font = .systemFont(ofSize: 13, weight: .semibold)
        heading.textColor = Palette.ink
        heading.lineBreakMode = .byTruncatingMiddle
        root.addArrangedSubview(heading)
        heading.widthAnchor.constraint(equalToConstant: 280).isActive = true
        let children = Set(folders.filter { $0.parts.starts(with: path) && $0.parts.count > path.count }
            .map { $0.parts[path.count] }).sorted { $0.localizedStandardCompare($1) == .orderedAscending }
        let list = TopAlignedStackView()
        list.orientation = .vertical
        list.alignment = .leading
        list.spacing = 2
        for child in children {
            let row = ActionButton(child + "  ›") { [weak self] in
                guard let self else { return }
                self.path.append(child)
                self.renderFolders()
            }
            row.alignment = .left
            row.heightAnchor.constraint(equalToConstant: 26).isActive = true
            row.widthAnchor.constraint(equalToConstant: 268).isActive = true
            list.addArrangedSubview(row)
            navigationButtons.append(row)
        }
        if children.isEmpty {
            let empty = NSTextField(labelWithString: "No subfolders")
            empty.textColor = Palette.muted
            list.addArrangedSubview(empty)
        }
        let scroll = NSScrollView()
        scroll.drawsBackground = false
        scroll.hasVerticalScroller = true
        scroll.autohidesScrollers = true
        scroll.scrollerStyle = .overlay
        scroll.documentView = list
        list.setFrameSize(NSSize(width: 280, height: max(26, children.count * 28 - 2)))
        root.addArrangedSubview(scroll)
        scroll.widthAnchor.constraint(equalToConstant: 280).isActive = true
        scroll.heightAnchor.constraint(equalToConstant: CGFloat(min(222, max(26, children.count * 28 - 2)))).isActive = true
        if let folder = folders.first(where: { $0.parts == path }) {
            let choose = ActionButton("Use this folder") { [weak self] in
                guard let self else { return }
                self.selectedFolderID = folder.id
                self.title = folder.parts.joined(separator: " / ")
                self.needsDisplay = true
                self.popover.close()
                self.onChange()
            }
            root.addArrangedSubview(choose)
            navigationButtons.append(choose)
        }
        let size = root.fittingSize
        let canvas = NSView(frame: NSRect(x: 0, y: 0, width: size.width + 32, height: size.height + 32))
        canvas.wantsLayer = true
        canvas.layer?.backgroundColor = Palette.canvas.cgColor
        root.frame = NSRect(x: 16, y: 16, width: size.width, height: size.height)
        root.autoresizingMask = [.width, .height]
        canvas.addSubview(root)
        controller.view = canvas
        popover.contentViewController = controller
        popover.contentSize = canvas.frame.size
        for (index, button) in navigationButtons.enumerated() {
            button.nextKeyView = navigationButtons[(index + 1) % navigationButtons.count]
            button.focusRingType = .none
            (button as? ActionButton)?.usesExplicitKeyLoop = true
        }
        if popover.isShown { focusBrowser() }
    }
}

@MainActor final class TopAlignedStackView: NSStackView {
    override var isFlipped: Bool { true }
}

@MainActor final class AppDelegate: NSObject, NSApplicationDelegate, UNUserNotificationCenterDelegate, NSTextFieldDelegate {
    let stateDirectory: URL = {
        if let index = CommandLine.arguments.firstIndex(of: "--state-dir"), CommandLine.arguments.count > index + 1 {
            return URL(fileURLWithPath: CommandLine.arguments[index + 1], isDirectory: true)
        }
        return FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/Application Support/Tuck")
    }()
    var statusItem: NSStatusItem!
    var statusLine = NSMenuItem(title: "Connecting…", action: nil, keyEquivalent: "")
    var pauseItem: NSMenuItem!
    var reviewItem: NSMenuItem!
    var window: NSWindow!
    var rows = TopAlignedStackView()
    var heading = NSTextField(labelWithString: "Making room.")
    var subtitle = NSTextField(labelWithString: "Getting your downloads ready…")
    var timer: Timer?
    var backend: Process?
    var lastStart = Date.distantPast
    var refreshing = false
    var watching = true
    var lastFingerprint = ""
    var pendingIDs = Set<String>()
    var lastNotification = Date.distantPast
    var notificationsEnabled = false
    var editors: [String: (NSTextField, FolderPicker)] = [:]
    var rowActions: [String: (NSButton, NSButton, NSTextField)] = [:]
    var movingIDs = Set<String>()
    var moveErrors: [String: String] = [:]

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        statusItem.button?.image = NSImage(systemSymbolName: "tray.and.arrow.down", accessibilityDescription: "Tuck")
        let menu = NSMenu()
        let brand = NSMenuItem(title: "Tuck", action: nil, keyEquivalent: "")
        brand.attributedTitle = NSAttributedString(string: "Tuck", attributes: [
            .foregroundColor: Palette.orange, .font: NSFont.systemFont(ofSize: 14, weight: .semibold)])
        menu.addItem(brand)
        menu.addItem(statusLine)
        menu.addItem(.separator())
        reviewItem = menu.addItem(withTitle: "Review downloads", action: #selector(showReview), keyEquivalent: "r")
        reviewItem.target = self
        reviewItem.image = NSImage(systemSymbolName: "tray", accessibilityDescription: nil)
        menu.addItem(.separator())
        pauseItem = menu.addItem(withTitle: "Pause suggestions", action: #selector(togglePause), keyEquivalent: "")
        pauseItem.target = self
        menu.addItem(withTitle: "Review older downloads…", action: #selector(scanExisting), keyEquivalent: "").target = self
        menu.addItem(withTitle: "Open settings folder", action: #selector(showSettings), keyEquivalent: "").target = self
        menu.addItem(.separator())
        menu.addItem(withTitle: "Quit Tuck", action: #selector(quit), keyEquivalent: "q").target = self
        statusItem.menu = menu
        buildWindow()
        if Bundle.main.bundleIdentifier != nil {
            UNUserNotificationCenter.current().delegate = self
            Task {
                notificationsEnabled = (try? await UNUserNotificationCenter.current().requestAuthorization(options: [.alert])) ?? false
            }
        }
        timer = Timer.scheduledTimer(withTimeInterval: 0.5, repeats: true) { _ in
            Task { @MainActor in await self.refresh() }
        }
        Task { await refresh() }
        if CommandLine.arguments.contains("--review") { showReview() }
    }

    func buildWindow() {
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 680, height: 560),
                          styleMask: [.titled, .closable, .resizable, .miniaturizable], backing: .buffered, defer: false)
        window.title = "Tuck"
        window.titleVisibility = .hidden
        window.titlebarAppearsTransparent = true
        window.backgroundColor = Palette.canvas
        window.appearance = NSAppearance(named: .aqua)
        window.isReleasedWhenClosed = false
        window.minSize = NSSize(width: 580, height: 420)
        window.maxSize = NSSize(width: 680, height: 560)
        window.center()
        let root = NSStackView()
        root.orientation = .vertical
        root.alignment = .leading
        root.spacing = 10
        root.edgeInsets = NSEdgeInsets(top: 14, left: 30, bottom: 20, right: 26)
        root.translatesAutoresizingMaskIntoConstraints = false
        window.contentView!.addSubview(root)
        NSLayoutConstraint.activate([root.leadingAnchor.constraint(equalTo: window.contentView!.leadingAnchor),
            root.trailingAnchor.constraint(equalTo: window.contentView!.trailingAnchor),
            root.topAnchor.constraint(equalTo: window.contentView!.topAnchor), root.bottomAnchor.constraint(equalTo: window.contentView!.bottomAnchor)])
        let brand = NSStackView()
        brand.spacing = 8
        brand.addArrangedSubview(symbol("tray.and.arrow.down.fill", color: Palette.orange, size: 16))
        let wordmark = caption("TUCK")
        wordmark.textColor = Palette.orange
        brand.addArrangedSubview(wordmark)
        let spacer = NSView()
        brand.addArrangedSubview(spacer)
        let privacy = label("●  Only on your Mac")
        privacy.textColor = Palette.muted
        privacy.font = .systemFont(ofSize: 10, weight: .medium)
        brand.addArrangedSubview(privacy)
        root.addArrangedSubview(brand)
        brand.widthAnchor.constraint(equalTo: root.widthAnchor, constant: -56).isActive = true
        root.setCustomSpacing(20, after: brand)
        heading.font = NSFont(name: "Georgia", size: 32) ?? .systemFont(ofSize: 32, weight: .semibold)
        heading.textColor = Palette.ink
        heading.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        root.addArrangedSubview(heading)
        subtitle.font = .systemFont(ofSize: 12)
        subtitle.textColor = Palette.muted
        subtitle.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        root.addArrangedSubview(subtitle)
        root.setCustomSpacing(20, after: subtitle)
        let scroll = NSScrollView()
        scroll.hasVerticalScroller = true
        scroll.autohidesScrollers = true
        scroll.scrollerStyle = .overlay
        scroll.drawsBackground = false
        rows.orientation = .vertical
        rows.alignment = .leading
        rows.spacing = 16
        rows.translatesAutoresizingMaskIntoConstraints = false
        scroll.documentView = rows
        root.addArrangedSubview(scroll)
        NSLayoutConstraint.activate([scroll.widthAnchor.constraint(equalTo: root.widthAnchor, constant: -56),
            rows.widthAnchor.constraint(equalTo: scroll.contentView.widthAnchor, constant: -4),
            rows.leadingAnchor.constraint(equalTo: scroll.contentView.leadingAnchor),
            rows.topAnchor.constraint(equalTo: scroll.contentView.topAnchor)])
    }

    func label(_ text: String) -> NSTextField {
        let field = NSTextField(wrappingLabelWithString: text)
        field.font = .systemFont(ofSize: 12)
        field.textColor = Palette.ink
        field.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        return field
    }
    func caption(_ text: String) -> NSTextField {
        let field = label(text)
        field.font = .systemFont(ofSize: 9, weight: .semibold)
        field.textColor = Palette.muted
        return field
    }
    func symbol(_ name: String, color: NSColor, size: CGFloat) -> NSImageView {
        let image = NSImageView(image: NSImage(systemSymbolName: name, accessibilityDescription: nil) ?? NSImage())
        image.contentTintColor = color
        image.symbolConfiguration = NSImage.SymbolConfiguration(pointSize: size, weight: .regular)
        image.widthAnchor.constraint(equalToConstant: size + 4).isActive = true
        image.heightAnchor.constraint(equalToConstant: size + 4).isActive = true
        return image
    }
    func panel(_ content: NSView, color: NSColor, inset: CGFloat, radius: CGFloat = 12) -> NSBox {
        let box = NSBox()
        box.boxType = .custom
        box.titlePosition = .noTitle
        box.fillColor = color
        box.borderWidth = 0
        box.cornerRadius = radius
        content.translatesAutoresizingMaskIntoConstraints = false
        box.addSubview(content)
        NSLayoutConstraint.activate([
            content.leadingAnchor.constraint(equalTo: box.leadingAnchor, constant: inset),
            content.trailingAnchor.constraint(equalTo: box.trailingAnchor, constant: -inset),
            content.topAnchor.constraint(equalTo: box.topAnchor, constant: inset),
            content.bottomAnchor.constraint(equalTo: box.bottomAnchor, constant: -inset)])
        return box
    }

    func request(_ path: String, body: [String: Any]? = nil) async throws -> [String: Any] {
        let sessionData = try Data(contentsOf: stateDirectory.appendingPathComponent("session.json"))
        guard let session = try JSONSerialization.jsonObject(with: sessionData) as? [String: Any],
              let port = session["port"] as? Int, (1...65535).contains(port), let token = session["token"] as? String else {
            throw NSError(domain: "DownloadSuggest", code: 1, userInfo: [NSLocalizedDescriptionKey: "Invalid local service session."])
        }
        var request = URLRequest(url: URL(string: "http://127.0.0.1:\(port)\(path)")!)
        request.timeoutInterval = path.hasSuffix("/apply") || path.hasSuffix("/undo") ? 3600 : 3
        request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        if let body {
            request.httpMethod = "POST"
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.httpBody = try JSONSerialization.data(withJSONObject: body)
        }
        let (data, response) = try await URLSession.shared.data(for: request)
        let result = (try JSONSerialization.jsonObject(with: data) as? [String: Any]) ?? [:]
        guard let response = response as? HTTPURLResponse, (200..<300).contains(response.statusCode) else {
            throw NSError(domain: "DownloadSuggest", code: 2,
                userInfo: [NSLocalizedDescriptionKey: result["error"] as? String ?? "Local service request failed."])
        }
        return result
    }

    func startBackend() throws {
        lastStart = Date()
        let data = try Data(contentsOf: stateDirectory.appendingPathComponent("runtime.json"))
        guard let runtime = try JSONSerialization.jsonObject(with: data) as? [String: String],
              let python = runtime["python"], let module = runtime["module_path"], let config = runtime["config"] else {
            throw NSError(domain: "DownloadSuggest", code: 3, userInfo: [NSLocalizedDescriptionKey: "Run the install script to configure the local service."])
        }
        let process = Process()
        process.executableURL = URL(fileURLWithPath: python)
        process.arguments = ["-m", "download_suggest", "serve", "--state-dir", stateDirectory.path, "--config", config]
        var environment = ProcessInfo.processInfo.environment
        environment["PYTHONPATH"] = module
        process.environment = environment
        process.standardOutput = FileHandle.nullDevice
        process.standardError = FileHandle.nullDevice
        try process.run()
        backend = process
    }

    func refresh() async {
        guard !refreshing else { return }
        refreshing = true
        defer { refreshing = false }
        do {
            let state = try await request("/v1/state")
            watching = state["watching"] as? Bool ?? true
            let pending = state["pending"] as? [[String: Any]] ?? []
            let recent = state["recent"] as? [[String: Any]] ?? []
            let ids = Set(pending.compactMap { $0["id"] as? String })
            statusItem.button?.title = pending.isEmpty ? "" : " \(pending.count)"
            statusLine.title = watching ? "Watching Downloads" : "Suggestions paused"
            statusLine.toolTip = state["model_status"] as? String
            reviewItem.title = pending.isEmpty ? "Review downloads" : "Review downloads (\(pending.count))"
            heading.stringValue = pending.isEmpty ? "All tucked away." : "Ready to file."
            subtitle.stringValue = pending.isEmpty ? "Nothing waiting for review." : "\(pending.count) download\(pending.count == 1 ? "" : "s") ready for your review."
            let historyReady = (state["audit_status"] as? String ?? "ok") == "ok"
            subtitle.textColor = historyReady ? Palette.muted : .systemRed
            if !historyReady {
                subtitle.stringValue = "Local history isn't recording. Check local settings."
                statusLine.title = "Local history needs attention"
            }
            pauseItem.title = watching ? "Pause suggestions" : "Resume suggestions"
            if !ids.subtracting(pendingIDs).isEmpty, Date().timeIntervalSince(lastNotification) >= 1 {
                notify(count: pending.count)
                lastNotification = Date()
            }
            pendingIDs = ids
            // Keep edits intact while polling; render only when the queue or folder choices change.
            let folders = state["folders"] as? [[String: Any]] ?? []
            let fingerprint = String(data: try JSONSerialization.data(withJSONObject: [pending, recent, folders], options: [.sortedKeys]), encoding: .utf8)!
            if fingerprint != lastFingerprint {
                lastFingerprint = fingerprint
                render(pending: pending, recent: recent, folders: folders)
            }
        } catch {
            if backend?.isRunning != true && Date().timeIntervalSince(lastStart) >= 5 {
                do { try startBackend() } catch { showConnectionError(error) }
            } else { showConnectionError(error) }
        }
    }

    func showConnectionError(_ error: Error) {
        statusLine.title = "Unable to connect"
        heading.stringValue = "One moment."
        subtitle.stringValue = "Couldn't connect. Check local settings from the menu."
        subtitle.toolTip = error.localizedDescription
        heading.toolTip = "Show local settings from the menu to check installation and configuration."
    }

    func render(pending: [[String: Any]], recent: [[String: Any]], folders: [[String: Any]]) {
        let drafts = editors.mapValues { ($0.0.stringValue, $0.1.selectedFolderID) }
        editors.removeAll()
        rowActions.removeAll()
        for view in rows.arrangedSubviews { rows.removeArrangedSubview(view); view.removeFromSuperview() }
        if pending.isEmpty {
            let empty = NSStackView()
            empty.orientation = .vertical
            empty.alignment = .centerX
            empty.spacing = 14
            empty.addArrangedSubview(symbol("tray.and.arrow.down", color: Palette.orange, size: 48))
            let title = label("Ready for the next one.")
            title.font = .systemFont(ofSize: 17, weight: .medium)
            empty.addArrangedSubview(title)
            let note = label("Keep Tuck open to catch new downloads.")
            note.textColor = Palette.muted
            empty.addArrangedSubview(note)
            let card = panel(empty, color: .white.withAlphaComponent(0.55), inset: 32, radius: 16)
            rows.addArrangedSubview(card)
            card.widthAnchor.constraint(equalTo: rows.widthAnchor).isActive = true
        }
        for item in pending {
            guard let id = item["id"] as? String else { continue }
            let group = NSStackView()
            group.orientation = .vertical
            group.alignment = .leading
            group.spacing = 9
            let sourceName = item["source_name"] as? String ?? "Download"
            let ext = (sourceName as NSString).pathExtension.lowercased()
            let icon = ["jpg", "jpeg", "png", "heic"].contains(ext) ? "photo" : (["zip", "gz", "tar"].contains(ext) ? "archivebox" : "doc.text")
            let fileHeader = NSStackView()
            fileHeader.spacing = 12
            fileHeader.addArrangedSubview(panel(symbol(icon, color: Palette.orange, size: 25), color: Palette.canvas, inset: 10))
            let fileDetails = NSStackView()
            fileDetails.orientation = .vertical
            fileDetails.alignment = .leading
            fileDetails.spacing = 4
            let original = label(sourceName)
            original.font = .systemFont(ofSize: 14, weight: .semibold)
            original.maximumNumberOfLines = 1
            original.lineBreakMode = .byTruncatingMiddle
            original.toolTip = sourceName
            fileDetails.addArrangedSubview(original)
            fileDetails.addArrangedSubview(caption((ext.isEmpty ? "FILE" : ext.uppercased()) + "  ·  IN DOWNLOADS"))
            fileHeader.addArrangedSubview(fileDetails)
            group.addArrangedSubview(fileHeader)
            fileHeader.widthAnchor.constraint(equalTo: group.widthAnchor).isActive = true
            group.setCustomSpacing(16, after: fileHeader)
            group.addArrangedSubview(caption("SAVE AS"))
            let name = NSTextField(string: drafts[id]?.0 ?? item["suggested_name"] as? String ?? "")
            name.setAccessibilityLabel("Suggested filename")
            name.font = .systemFont(ofSize: 16, weight: .medium)
            name.textColor = Palette.ink
            name.isBezeled = false
            name.drawsBackground = false
            name.focusRingType = .default
            name.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
            name.cell?.isScrollable = true
            name.delegate = self
            let namePanel = panel(name, color: Palette.canvas.withAlphaComponent(0.6), inset: 10, radius: 8)
            group.addArrangedSubview(namePanel)
            namePanel.widthAnchor.constraint(equalTo: group.widthAnchor).isActive = true
            group.setCustomSpacing(12, after: namePanel)
            let destination = FolderPicker(folders: folders, selectedID: drafts[id]?.1 ?? item["folder_id"] as? String)
            editors[id] = (name, destination)
            destination.onChange = { [weak self] in self?.updateMoveButtons() }
            let destinationGroup = NSStackView()
            destinationGroup.orientation = .vertical
            destinationGroup.alignment = .leading
            destinationGroup.spacing = 2
            destinationGroup.addArrangedSubview(caption("↓  MOVE TO"))
            let destinationRow = NSStackView()
            destinationRow.spacing = 6
            let folderIcon = symbol("folder", color: Palette.muted, size: 16)
            destinationRow.addArrangedSubview(folderIcon)
            destinationRow.addArrangedSubview(destination)
            destination.widthAnchor.constraint(equalTo: destinationRow.widthAnchor, constant: -26).isActive = true
            destination.heightAnchor.constraint(equalToConstant: 24).isActive = true
            destinationGroup.addArrangedSubview(destinationRow)
            destinationRow.widthAnchor.constraint(equalTo: destinationGroup.widthAnchor).isActive = true
            let destinationPanel = panel(destinationGroup, color: Palette.sage, inset: 10, radius: 9)
            group.addArrangedSubview(destinationPanel)
            destinationPanel.widthAnchor.constraint(equalTo: group.widthAnchor).isActive = true
            let reason = label(item["reason"] as? String ?? "A suggestion, always your choice.")
            reason.textColor = Palette.muted
            reason.font = .systemFont(ofSize: 11)
            reason.maximumNumberOfLines = 2
            group.addArrangedSubview(reason)
            reason.widthAnchor.constraint(equalTo: group.widthAnchor).isActive = true
            group.setCustomSpacing(14, after: reason)
            let actions = NSStackView()
            actions.spacing = 14
            let move = ActionButton("Move file  →") { [weak self] in self?.moveItem(id) }
            move.primary = true
            move.heightAnchor.constraint(equalToConstant: 42).isActive = true
            move.widthAnchor.constraint(equalToConstant: 148).isActive = true
            actions.addArrangedSubview(move)
            let leave = ActionButton("Leave here") { [weak self] in self?.mutate("/v1/items/\(id)/leave") }
            actions.addArrangedSubview(leave)
            let reveal = ActionButton("Show in Finder ↗") {
                if let path = item["source_path"] as? String { NSWorkspace.shared.activateFileViewerSelecting([URL(fileURLWithPath: path)]) }
            }
            actions.addArrangedSubview(reveal)
            group.addArrangedSubview(actions)
            let progress = label("")
            group.addArrangedSubview(progress)
            progress.widthAnchor.constraint(equalTo: group.widthAnchor).isActive = true
            rowActions[id] = (move, leave, progress)
            let card = panel(group, color: .white, inset: 20, radius: 16)
            rows.addArrangedSubview(card)
            card.widthAnchor.constraint(equalTo: rows.widthAnchor).isActive = true
        }
        updateMoveButtons()
        if let item = recent.first(where: { $0["status"] as? String == "applied" }), let id = item["id"] as? String {
            let row = NSStackView()
            row.spacing = 8
            row.addArrangedSubview(symbol("checkmark.circle", color: Palette.muted, size: 12))
            let filedName = (item["applied_path"] as? String).map { URL(fileURLWithPath: $0).lastPathComponent }
            let note = label("Filed \(filedName ?? item["source_name"] as? String ?? "your file")")
            note.font = .systemFont(ofSize: 11)
            note.maximumNumberOfLines = 1
            note.lineBreakMode = .byTruncatingMiddle
            note.textColor = Palette.muted
            row.addArrangedSubview(note)
            row.addArrangedSubview(ActionButton("Undo") { [weak self] in self?.mutate("/v1/items/\(id)/undo") })
            rows.addArrangedSubview(row)
            row.widthAnchor.constraint(equalTo: rows.widthAnchor).isActive = true
        }
        for item in recent where item["status"] as? String == "interrupted" {
            let note = label("Needs your attention · \(item["source_name"] as? String ?? "File")")
            note.textColor = Palette.orange
            rows.addArrangedSubview(note)
            rows.addArrangedSubview(label(item["reason"] as? String ?? "Review these locations to recover the interrupted move."))
            for (index, path) in (item["recovery_paths"] as? [String] ?? []).enumerated() {
                let reveal = ActionButton("Reveal recovery location \(index + 1)") {
                    var url = URL(fileURLWithPath: path).standardizedFileURL
                    while !FileManager.default.fileExists(atPath: url.path), url.path != "/" {
                        url.deleteLastPathComponent()
                    }
                    NSWorkspace.shared.activateFileViewerSelecting([url])
                }
                reveal.toolTip = path
                reveal.setAccessibilityHelp(path)
                rows.addArrangedSubview(reveal)
            }
        }
    }

    func controlTextDidChange(_ notification: Notification) { updateMoveButtons() }

    @objc func updateMoveButtons() {
        for (id, (name, destination)) in editors {
            guard let (move, leave, progress) = rowActions[id] else { continue }
            let moving = movingIDs.contains(id)
            move.isEnabled = !moving && destination.selectedFolderID != nil
                && !name.stringValue.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
            move.title = moving ? "Moving…" : "Move file  →"
            move.toolTip = destination.selectedFolderID == nil ? "Choose a destination first" : nil
            leave.isEnabled = !moving
            name.isEnabled = !moving
            destination.isEnabled = !moving
            progress.stringValue = moving ? "Moving your file safely…" : moveErrors[id] ?? ""
            progress.textColor = moving ? .secondaryLabelColor : .systemRed
            progress.isHidden = progress.stringValue.isEmpty
        }
    }

    func moveItem(_ id: String) {
        guard !movingIDs.contains(id), let (name, destination) = editors[id],
              let folder = destination.selectedFolderID,
              !name.stringValue.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { return }
        let body = ["folder_id": folder, "name": name.stringValue]
        movingIDs.insert(id)
        moveErrors[id] = nil
        updateMoveButtons()
        Task {
            do { _ = try await request("/v1/items/\(id)/apply", body: body) }
            catch { moveErrors[id] = error.localizedDescription }
            movingIDs.remove(id)
            updateMoveButtons()
            await refresh()
        }
    }

    func mutate(_ path: String, body: [String: Any] = [:]) {
        Task {
            do { _ = try await request(path, body: body); await refresh() }
            catch { showError(error.localizedDescription) }
        }
    }
    func showError(_ message: String) {
        let alert = NSAlert()
        alert.messageText = "Couldn't complete that action"
        alert.informativeText = message
        alert.beginSheetModal(for: window)
    }
    func notify(count: Int) {
        guard notificationsEnabled else { return }
        let content = UNMutableNotificationContent()
        content.title = "Tuck"
        content.body = "\(count) download\(count == 1 ? "" : "s") ready to review."
        // One replaceable notification, with no private names or paths on the lock screen.
        UNUserNotificationCenter.current().add(UNNotificationRequest(identifier: "review-downloads", content: content, trigger: nil))
    }
    nonisolated func userNotificationCenter(_ center: UNUserNotificationCenter, didReceive response: UNNotificationResponse,
                                           withCompletionHandler completionHandler: @escaping () -> Void) {
        Task { @MainActor in self.showReview() }
        completionHandler()
    }
    @objc func showReview() { window.makeKeyAndOrderFront(nil); NSApp.activate(ignoringOtherApps: true) }
    @objc func togglePause() { mutate("/v1/pause", body: ["paused": watching]) }
    @objc func scanExisting() {
        showReview()
        let alert = NSAlert()
        alert.messageText = "Review files already in Downloads?"
        alert.informativeText = "This adds existing files to the review queue. Nothing is moved automatically."
        alert.addButton(withTitle: "Scan")
        alert.addButton(withTitle: "Cancel")
        alert.beginSheetModal(for: window) { response in
            if response == .alertFirstButtonReturn { self.mutate("/v1/scan") }
        }
    }
    @objc func showSettings() { NSWorkspace.shared.open(stateDirectory) }
    @objc func quit() { NSApp.terminate(nil) }
    func applicationWillTerminate(_ notification: Notification) {
        timer?.invalidate()
        if let backend, backend.isRunning { backend.terminate() }
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.run()
