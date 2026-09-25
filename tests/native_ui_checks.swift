// AppKit integration checks with fictional data, appended to the app types by test_e2e.py.
let app = NSApplication.shared
app.setActivationPolicy(.accessory)
let delegate = AppDelegate()
delegate.buildWindow()
let folders: [[String: Any]] = [
    ["id": "research", "label": "Research"],
    ["id": "papers", "label": "Research / Papers"],
    ["id": "notes", "label": "Research / Papers / Notes"],
    ["id": "work", "label": "Work"]
] + (0..<30).map { ["id": "project-\($0)", "label": String(format: "Work / Project %02d", $0)] }
delegate.render(pending: [["id": "example", "source_name": "Example.txt", "suggested_name": "Example.txt", "folder_id": "research"]],
    recent: [["id": "filed", "status": "applied", "applied_path": "/tmp/Chosen.txt", "suggested_name": "Suggested.txt"]], folders: folders)
delegate.window.makeKeyAndOrderFront(nil)
delegate.window.contentView!.layoutSubtreeIfNeeded()
let picker = delegate.editors["example"]!.1
assert(picker.frame.width > 300)
@MainActor func descendants(_ view: NSView) -> [NSView] { [view] + view.subviews.flatMap(descendants) }
@MainActor func button(_ title: String) -> NSButton {
    let matches = app.windows.flatMap { $0.contentView.map(descendants) ?? [] }
        .compactMap { $0 as? NSButton }.filter { $0.title == title && $0.window?.isVisible == true }
    assert(matches.count == 1, "Expected one visible button named \(title)")
    return matches[0]
}
@MainActor func key(_ code: UInt16, _ characters: String, shift: Bool = false) -> NSEvent {
    NSEvent.keyEvent(with: .keyDown, location: .zero, modifierFlags: shift ? .shift : [], timestamp: 0,
                    windowNumber: 0, context: nil, characters: characters,
                    charactersIgnoringModifiers: characters, isARepeat: false, keyCode: code)!
}
assert(descendants(delegate.rows).compactMap { $0 as? NSTextField }.contains { $0.stringValue == "Filed Chosen.txt" })
picker.performClick(nil)
let research = button("Research  ›")
assert(research.window?.firstResponder === research)
let folderStack = research.enclosingScrollView!.superview!
assert(folderStack.frame.minX >= 16, "Folder browser needs left padding")
research.keyDown(with: key(48, "\t"))
let work = button("Work  ›")
assert(work.window?.firstResponder === work)
work.keyDown(with: key(48, "\t", shift: true))
assert(research.window?.firstResponder === research)
assert(research.performKeyEquivalent(with: key(36, "\r")))
button("Papers  ›").performClick(nil)
button("Notes  ›").performClick(nil)
button("Use this folder").performClick(nil)
assert(picker.selectedFolderID == "notes")
picker.performClick(nil)
button("Work  ›").performClick(nil)
let first = button("Project 00  ›")
first.window?.makeFirstResponder(first)
for _ in 0..<29 {
    (first.window!.firstResponder as! NSButton).keyDown(with: key(48, "\t"))
}
let last = button("Project 29  ›")
assert(last.window?.firstResponder === last)
assert(!last.visibleRect.isEmpty, "Keyboard focus must scroll into view")
last.keyDown(with: key(49, " "))
button("‹  Back").performClick(nil)
button("Use this folder").performClick(nil)
assert(picker.selectedFolderID == "work")
assert(delegate.rowActions["example"]!.0.isEnabled)
delegate.window.close()
print("Native folder navigation, keyboard focus, scrolling, selection and layout passed")
