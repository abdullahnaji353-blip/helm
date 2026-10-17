import AVFoundation
import AppKit
import ApplicationServices
import CoreGraphics
import Security
import SwiftUI
import os

private enum DeviceControlError: Error {
    case unsupportedAction(String)
    case missingAccessibilityPermission
    case invalidCoordinates
    case invalidText
}

private func deviceControlKillSwitchCallback(
    proxy: CGEventTapProxy,
    type: CGEventType,
    event: CGEvent,
    refcon: UnsafeMutableRawPointer?
) -> Unmanaged<CGEvent>? {
    let flags = event.flags
    let keyCode = event.getIntegerValueField(.keyboardEventKeycode)
    let isHotkey = type == .keyDown && keyCode == 40 && flags.contains(.maskCommand) && flags.contains(.maskControl)
    if isHotkey {
        DeviceActionController.stopRequested = true
        return nil
    }
    return Unmanaged.passRetained(event)
}

private final class DeviceActionController {
    fileprivate static var stopRequested = false

    static func installKillSwitch() {
        let mask = CGEventMask((1 << CGEventType.keyDown.rawValue) | (1 << CGEventType.flagsChanged.rawValue))
        guard let tap = CGEvent.tapCreate(
            tap: .cgSessionEventTap,
            place: .headInsertEventTap,
            options: .listenOnly,
            eventsOfInterest: mask,
            callback: deviceControlKillSwitchCallback,
            userInfo: nil
        ) else {
            return
        }
        let source = CFMachPortCreateRunLoopSource(kCFAllocatorDefault, tap, 0)
        CFRunLoopAddSource(CFRunLoopGetMain(), source, .commonModes)
        CGEvent.tapEnable(tap: tap, enable: true)
    }

    static func shouldStop() -> Bool {
        let value = stopRequested
        stopRequested = false
        return value
    }

    static func runCLI(arguments: [String]) -> Int32 {
        let args = arguments.first == "--device-action" ? Array(arguments.dropFirst()) : arguments
        guard AXIsProcessTrusted() else {
            fputs("DEVICE_CONTROL_ERROR: Accessibility permission is required.\n", stderr)
            return 2
        }

        guard args.count >= 2 else {
            fputs("DEVICE_CONTROL_ERROR: usage: --device-action <open|click|type|kill> [args...]\n", stderr)
            return 1
        }

        do {
            let action = args[0]
            switch action {
            case "open":
                let target = args[1]
                guard (try? openApp(named: target)) == true else {
                    fputs("DEVICE_CONTROL_ERROR: could not open \(target)\n", stderr)
                    return 3
                }
            case "click":
                guard args.count >= 3 else {
                    throw DeviceControlError.invalidCoordinates
                }
                let x = Int(args[1]) ?? 0
                let y = Int(args[2]) ?? 0
                guard click(x: x, y: y) else {
                    fputs("DEVICE_CONTROL_ERROR: click failed\n", stderr)
                    return 4
                }
            case "type":
                guard args.count >= 2 else {
                    throw DeviceControlError.invalidText
                }
                let text = args[1]
                guard typeText(text) else {
                    fputs("DEVICE_CONTROL_ERROR: key injection failed\n", stderr)
                    return 5
                }
            case "kill":
                stopRequested = true
            default:
                throw DeviceControlError.unsupportedAction(action)
            }
            return 0
        } catch DeviceControlError.unsupportedAction(let name) {
            fputs("DEVICE_CONTROL_ERROR: unsupported action \(name)\n", stderr)
            return 1
        } catch DeviceControlError.missingAccessibilityPermission {
            fputs("DEVICE_CONTROL_ERROR: Accessibility permission is required.\n", stderr)
            return 2
        } catch DeviceControlError.invalidCoordinates {
            fputs("DEVICE_CONTROL_ERROR: click requires x y integer coordinates\n", stderr)
            return 1
        } catch DeviceControlError.invalidText {
            fputs("DEVICE_CONTROL_ERROR: type requires a string payload\n", stderr)
            return 1
        } catch {
            fputs("DEVICE_CONTROL_ERROR: \(error.localizedDescription)\n", stderr)
            return 1
        }
    }

    private static func openApp(named target: String) throws -> Bool {
        let name = target.lowercased()
        let bundleIDs: [String: String] = [
            "safari": "com.apple.Safari",
            "finder": "com.apple.finder",
            "notes": "com.apple.Notes",
            "calendar": "com.apple.iCal",
            "calculator": "com.apple.calculator",
        ]
        if let bundleID = bundleIDs[name] {
            let appURL = NSWorkspace.shared.urlForApplication(withBundleIdentifier: bundleID)
            if let appURL {
                let config = NSWorkspace.OpenConfiguration()
                config.hidesOthers = false
                NSWorkspace.shared.openApplication(at: appURL, configuration: config) { _, _ in }
                return true
            }
        }
        return NSWorkspace.shared.launchApplication(target)
    }

    private static func click(x: Int, y: Int) -> Bool {
        let point = CGPoint(x: x, y: y)
        let source = CGEventSource(stateID: .hidSystemState)
        let move = CGEvent(mouseEventSource: source, mouseType: .mouseMoved, mouseCursorPosition: point, mouseButton: .left)
        let down = CGEvent(mouseEventSource: source, mouseType: .leftMouseDown, mouseCursorPosition: point, mouseButton: .left)
        let up = CGEvent(mouseEventSource: source, mouseType: .leftMouseUp, mouseCursorPosition: point, mouseButton: .left)
        move?.post(tap: .cghidEventTap)
        down?.post(tap: .cghidEventTap)
        up?.post(tap: .cghidEventTap)
        return true
    }

    private static func typeText(_ text: String) -> Bool {
        let keyMap: [Character: CGKeyCode] = [
            "a": 0x00, "b": 0x0B, "c": 0x08, "d": 0x02, "e": 0x0E, "f": 0x03, "g": 0x05, "h": 0x04,
            "i": 0x22, "j": 0x26, "k": 0x28, "l": 0x25, "m": 0x2E, "n": 0x2D, "o": 0x1F, "p": 0x23,
            "q": 0x0C, "r": 0x0F, "s": 0x01, "t": 0x11, "u": 0x20, "v": 0x09, "w": 0x0D, "x": 0x07,
            "y": 0x10, "z": 0x06,
            "A": 0x00, "B": 0x0B, "C": 0x08, "D": 0x02, "E": 0x0E, "F": 0x03, "G": 0x05, "H": 0x04,
            "I": 0x22, "J": 0x26, "K": 0x28, "L": 0x25, "M": 0x2E, "N": 0x2D, "O": 0x1F, "P": 0x23,
            "Q": 0x0C, "R": 0x0F, "S": 0x01, "T": 0x11, "U": 0x20, "V": 0x09, "W": 0x0D, "X": 0x07,
            "Y": 0x10, "Z": 0x06,
            "0": 0x1D, "1": 0x12, "2": 0x13, "3": 0x14, "4": 0x15, "5": 0x17, "6": 0x16, "7": 0x1A,
            "8": 0x1C, "9": 0x19, " ": 0x31, ".": 0x2F, ",": 0x2B, "-": 0x1B, "/": 0x2C, "_": 0x1B,
            ":": 0x29, ";": 0x29, "'": 0x27, "\"": 0x27, "(": 0x1A, ")": 0x1A, "[": 0x21, "]": 0x21,
            "!": 0x12, "?": 0x2C, "@": 0x12, "#": 0x14, "$": 0x15, "%": 0x17, "&": 0x1A, "*": 0x1C,
            "+": 0x18, "=": 0x18, "\\": 0x2A, "|": 0x2A, "<": 0x2B, ">": 0x2F, "~": 0x1B,
        ]
        let source = CGEventSource(stateID: .hidSystemState)
        for character in text {
            guard let code = keyMap[character] else { continue }
            let isUpper = character.isUppercase
            let flags: CGEventFlags = isUpper ? [.maskShift] : []
            let down = CGEvent(keyboardEventSource: source, virtualKey: CGKeyCode(code), keyDown: true)
            let up = CGEvent(keyboardEventSource: source, virtualKey: CGKeyCode(code), keyDown: false)
            down?.flags = flags
            up?.flags = flags
            down?.post(tap: .cghidEventTap)
            up?.post(tap: .cghidEventTap)
        }
        return true
    }
}

private enum StartupDiagnostics {
    private static let logger = Logger(
        subsystem: "local.desktop-assistant",
        category: "startup"
    )

    static func log() {
        let bundle = Bundle.main
        let signing = signingDetails()
        logger.info(
            """
            startup AXIsProcessTrusted=\(AXIsProcessTrusted(), privacy: .public) \
            bundleIdentifier=\(bundle.bundleIdentifier ?? "unknown", privacy: .public) \
            bundlePath=\(bundle.bundlePath, privacy: .public) \
            appSandbox=\(signing.sandbox, privacy: .public) \
            signingIdentity=\(signing.identity, privacy: .public) \
            adHocSigned=\(signing.adHoc, privacy: .public)
            """
        )
    }

    private static func signingDetails() -> (
        identity: String,
        sandbox: String,
        adHoc: String
    ) {
        var code: SecStaticCode?
        guard SecStaticCodeCreateWithPath(
            Bundle.main.bundleURL as CFURL,
            [],
            &code
        ) == errSecSuccess, let code else {
            return ("unavailable", "unavailable", "unavailable")
        }

        var information: CFDictionary?
        guard SecCodeCopySigningInformation(
            code,
            SecCSFlags(rawValue: kSecCSSigningInformation),
            &information
        ) == errSecSuccess,
              let values = information as? [String: Any] else {
            return ("unavailable", "unavailable", "unavailable")
        }

        let identifier = values[kSecCodeInfoIdentifier as String] as? String ?? "unknown"
        let team = values[kSecCodeInfoTeamIdentifier as String] as? String
        let identity = team.map { "\(identifier) (team \($0))" } ?? identifier
        let entitlements = values[kSecCodeInfoEntitlementsDict as String] as? [String: Any]
        let sandbox = entitlements?["com.apple.security.app-sandbox"] as? Bool == true
            ? "enabled"
            : "disabled"
        let flags = (values[kSecCodeInfoFlags as String] as? NSNumber)?.uint32Value ?? 0
        let adHoc = flags & 0x2 == 0x2 ? "yes" : "no"
        return (identity, sandbox, adHoc)
    }
}

@main
struct MacMenuAssistantApp: App {
    @StateObject private var controller = TaskController()
    @StateObject private var chat = ChatController()

    init() {
        NSApplication.shared.setActivationPolicy(.accessory)
        StartupDiagnostics.log()
    }

    var body: some Scene {
        Window("Chatbot", id: "hud") {
            HUDView()
                .environmentObject(controller)
                .environmentObject(chat)
                .onAppear { NSApp.activate(ignoringOtherApps: true) }
        }
        .defaultSize(width: 1100, height: 700)
        MenuBarExtra("Chatbot", systemImage: chat.isListening ? "waveform" : "circle.hexagongrid.fill") {
            ChatRootView()
                .environmentObject(controller)
                .environmentObject(chat)
                .frame(width: 420, height: 640)
        }
        .menuBarExtraStyle(.window)
    }
}

enum TaskAutonomy: String, CaseIterable, Identifiable {
    case approval
    case autonomous

    var id: String { rawValue }

    var title: String {
        switch self {
        case .approval: "Ask for approval"
        case .autonomous: "Act autonomously"
        }
    }

    var detail: String {
        switch self {
        case .approval: "Review each proposed desktop action."
        case .autonomous: "Run this task without action-by-action prompts."
        }
    }
}

enum AssistantMode: String, CaseIterable, Identifiable {
    case ask
    case control
    case mcp

    var id: String { rawValue }

    var title: String {
        switch self {
        case .ask: "Ask"
        case .control: "Control"
        case .mcp: "MCP Tools"
        }
    }
}

enum JSONValue: Codable, Hashable {
    case string(String)
    case number(Double)
    case bool(Bool)
    case object([String: JSONValue])
    case array([JSONValue])
    case null

    init(from decoder: Decoder) throws {
        let container = try decoder.singleValueContainer()
        if container.decodeNil() {
            self = .null
        } else if let value = try? container.decode(Bool.self) {
            self = .bool(value)
        } else if let value = try? container.decode(Double.self) {
            self = .number(value)
        } else if let value = try? container.decode(String.self) {
            self = .string(value)
        } else if let value = try? container.decode([String: JSONValue].self) {
            self = .object(value)
        } else if let value = try? container.decode([JSONValue].self) {
            self = .array(value)
        } else {
            throw DecodingError.dataCorruptedError(
                in: container,
                debugDescription: "Unsupported JSON value"
            )
        }
    }

    func encode(to encoder: Encoder) throws {
        var container = encoder.singleValueContainer()
        switch self {
        case .string(let value): try container.encode(value)
        case .number(let value): try container.encode(value)
        case .bool(let value): try container.encode(value)
        case .object(let value): try container.encode(value)
        case .array(let value): try container.encode(value)
        case .null: try container.encodeNil()
        }
    }
}

struct MCPTool: Decodable, Identifiable, Hashable {
    let name: String
    let description: String
    let inputSchema: [String: JSONValue]
    let server: String

    var id: String { "\(server).\(name)" }
}

private struct MCPConnectionResponse: Decodable {
    struct Server: Decodable {
        struct Tool: Decodable {
            let name: String
            let description: String?
            let inputSchema: [String: JSONValue]?
        }

        let name: String
        let tools: [Tool]
    }

    let servers: [Server]
    let errors: [String: String]
}

@MainActor
final class TaskController: ObservableObject {
    @Published var task = ""
    @Published var mode: AssistantMode = .ask
    @Published var autonomy: TaskAutonomy = .approval
    @Published var researchWeb = false
    @Published var output = ""
    @Published var pendingApproval: String?
    @Published var isRunning = false
    @Published var errorMessage: String?
    @Published var isListening = false
    @Published var voiceStatus = "Hold Control+Option to speak."
    @Published var hasScreenCaptureAccess = CGPreflightScreenCaptureAccess()
    @Published var mcpConfig = """
    {
      "mcpServers": {}
    }
    """
    @Published var mcpTools: [MCPTool] = []
    @Published var selectedMCPToolID = ""
    @Published var mcpArguments = "{}"
    @Published var mcpStatus = "Configure a local MCP server to get started."
    @Published var showingMCPConfirmation = false

    var menuBarTitle: String {
        if isListening { return "Listening" }
        if isRunning { return "Task running" }
        return "Assistant"
    }

    var menuBarSymbol: String {
        isListening ? "waveform" : isRunning ? "ellipsis.circle" : "waveform"
    }

    private var process: Process?
    private var inputPipe: Pipe?
    private var outputBuffer = ""
    private let voiceInput = VoiceInputController()
    private let speechSynthesizer = AVSpeechSynthesizer()
    private let mcpLogger = Logger(
        subsystem: "local.desktop-assistant",
        category: "mcp"
    )
    private var voiceConfigured = false
    private var runningOperation: String?

    init() {
        do {
            let appSupport = try FileManager.default.url(
                for: .applicationSupportDirectory,
                in: .userDomainMask,
                appropriateFor: nil,
                create: true
            )
            let configURL = appSupport
                .appendingPathComponent("Local Desktop Assistant", isDirectory: true)
                .appendingPathComponent("mcp_servers.json")
            if FileManager.default.fileExists(atPath: configURL.path) {
                mcpConfig = try String(contentsOf: configURL, encoding: .utf8)
            } else {
                let root = ProcessInfo.processInfo.environment["ASSISTANT_PROJECT_DIR"]
                    .map { URL(fileURLWithPath: $0, isDirectory: true) }
                    ?? URL(fileURLWithPath: #filePath)
                        .deletingLastPathComponent()
                        .deletingLastPathComponent()
                        .deletingLastPathComponent()
                mcpConfig = try Self.defaultMCPConfig(projectRoot: root)
            }
        } catch {
            errorMessage = "Could not initialize MCP settings: \(error.localizedDescription)"
        }
    }

    private static func defaultMCPConfig(projectRoot: URL) throws -> String {
        let config: [String: Any] = [
            "mcpServers": [
                "local-ping": [
                    "command": projectRoot.appendingPathComponent(".venv/bin/python").path,
                    "args": [projectRoot.appendingPathComponent("mcp_ping_server.py").path],
                    "cwd": projectRoot.path,
                ]
            ]
        ]
        let data = try JSONSerialization.data(
            withJSONObject: config,
            options: [.prettyPrinted, .sortedKeys]
        )
        guard let value = String(data: data, encoding: .utf8) else {
            throw NSError(
                domain: "LocalDesktopAssistant.MCP",
                code: 2,
                userInfo: [NSLocalizedDescriptionKey: "Could not encode the built-in MCP test configuration."]
            )
        }
        return value
    }

    func configureVoice() {
        guard !voiceConfigured else { return }
        voiceConfigured = true
        voiceInput.onStatusChange = { [weak self] listening, status in
            self?.isListening = listening
            self?.voiceStatus = status
        }
        voiceInput.onError = { [weak self] message in
            self?.errorMessage = message
        }
        voiceInput.onPartialTranscript = { [weak self] transcript in
            self?.output = "Listening: \(transcript)\n"
        }
        voiceInput.onTranscript = { [weak self] transcript, hotkey in
            guard let self else { return }
            self.task = transcript
            self.output = "Heard: \(transcript)\n"
            if hotkey || self.mode == .control {
                self.mode = .control
                self.autonomy = .autonomous
            } else {
                self.mode = .ask
            }
            self.startTask()
        }
        voiceInput.installHotkey()
    }

    func toggleVoiceCapture() {
        voiceInput.toggleCapture()
    }

    func retryVoiceShortcut() {
        voiceInput.installHotkey()
    }

    func refreshScreenCaptureAccess() {
        hasScreenCaptureAccess = CGPreflightScreenCaptureAccess()
    }

    func requestScreenCaptureAccess() {
        hasScreenCaptureAccess = CGRequestScreenCaptureAccess()
    }

    func openScreenCaptureSettings() {
        guard let settingsURL = URL(
            string: "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture"
        ) else {
            errorMessage = "Could not open Screen Recording settings."
            return
        }
        guard NSWorkspace.shared.open(settingsURL) else {
            errorMessage = "Could not open Screen Recording settings."
            return
        }
    }

    func startTask() {
        guard !isRunning else {
            errorMessage = "A task is already running. Stop it before starting another."
            return
        }
        if mode == .mcp {
            connectMCP()
            return
        }
        let requestedTask = task.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !requestedTask.isEmpty else {
            errorMessage = "Enter or speak a request first."
            return
        }

        var arguments: [String]
        if mode == .ask {
            arguments = ["--chat", requestedTask]
        } else {
            arguments = ["--task", requestedTask, "--autonomy", autonomy.rawValue]
        }
        if researchWeb {
            arguments.append("--research-web")
        }
        launchBackend(arguments: arguments, initialOutput: "Request: \(requestedTask)\n")
    }

    func testScreenCapture() {
        launchBackend(
            arguments: ["--test-screen-capture"],
            initialOutput: "Testing screen capture used by Control…\n"
        )
    }

    func connectMCP() {
        do {
            let configPath = try saveMCPConfig()
            mcpLogger.info("Connect requested config=\(configPath.path, privacy: .public)")
            mcpTools = []
            mcpStatus = "Connecting to configured MCP servers…"
            launchBackend(
                arguments: ["--mcp-list", "--mcp-config", configPath.path],
                initialOutput: "Connecting to MCP servers…\n"
            )
        } catch {
            errorMessage = "Could not save MCP configuration: \(error.localizedDescription)"
        }
    }

    func requestMCPToolCall() {
        showingMCPConfirmation = true
    }

    func confirmMCPToolCall() {
        guard let tool = mcpTools.first(where: { $0.id == selectedMCPToolID }) else {
            errorMessage = "Choose an MCP tool first."
            return
        }
        guard let data = mcpArguments.data(using: .utf8),
              let value = try? JSONSerialization.jsonObject(with: data),
              value is [String: Any] else {
            errorMessage = "Tool arguments must be a valid JSON object."
            return
        }
        do {
            let configPath = try saveMCPConfig()
            let argumentsJSON = String(data: data, encoding: .utf8) ?? "{}"
            mcpLogger.info(
                "Tool call requested server=\(tool.server, privacy: .public) tool=\(tool.name, privacy: .public)"
            )
            launchBackend(
                arguments: [
                    "--mcp-call", tool.server, tool.name,
                    "--arguments", argumentsJSON,
                    "--mcp-config", configPath.path
                ],
                initialOutput: "Calling \(tool.server).\(tool.name)…\n"
            )
        } catch {
            errorMessage = "Could not save MCP configuration: \(error.localizedDescription)"
        }
    }

    private func saveMCPConfig() throws -> URL {
        let data = Data(mcpConfig.utf8)
        let decoded = try JSONSerialization.jsonObject(with: data)
        guard let root = decoded as? [String: Any],
              root["mcpServers"] is [String: Any] else {
            throw NSError(
                domain: "LocalDesktopAssistant.MCP",
                code: 1,
                userInfo: [NSLocalizedDescriptionKey: "Config must include an mcpServers object."]
            )
        }
        let appSupport = try FileManager.default.url(
            for: .applicationSupportDirectory,
            in: .userDomainMask,
            appropriateFor: nil,
            create: true
        )
        let directory = appSupport.appendingPathComponent(
            "Local Desktop Assistant",
            isDirectory: true
        )
        try FileManager.default.createDirectory(
            at: directory,
            withIntermediateDirectories: true
        )
        try FileManager.default.setAttributes(
            [.posixPermissions: 0o700],
            ofItemAtPath: directory.path
        )
        let configURL = directory.appendingPathComponent("mcp_servers.json")
        try data.write(to: configURL, options: .atomic)
        try FileManager.default.setAttributes(
            [.posixPermissions: 0o600],
            ofItemAtPath: configURL.path
        )
        return configURL
    }

    private func launchBackend(arguments: [String], initialOutput: String) {
        guard !isRunning else { return }
        let root = projectRoot
        let python = root.appendingPathComponent(".venv/bin/python")
        let script = root.appendingPathComponent("assistant.py")
        guard FileManager.default.isExecutableFile(atPath: python.path) else {
            errorMessage = "Project Python not found. Set up .venv using the README instructions."
            return
        }
        guard FileManager.default.fileExists(atPath: script.path) else {
            errorMessage = "Could not find assistant.py in \(root.path)."
            return
        }

        output = initialOutput
        outputBuffer = ""
        errorMessage = nil
        pendingApproval = nil
        let operation = arguments.first(where: {
            ["--mcp-list", "--mcp-call"].contains($0)
        }) ?? "task"
        runningOperation = operation
        if operation != "task" {
            mcpLogger.info(
                "Launching Python MCP backend operation=\(operation, privacy: .public) executable=\(python.path, privacy: .public) script=\(script.path, privacy: .public)"
            )
        }

        let child = Process()
        let stdout = Pipe()
        let stdin = Pipe()
        child.executableURL = python
        child.arguments = ["-u", script.path] + arguments
        child.currentDirectoryURL = root
        child.standardOutput = stdout
        child.standardError = stdout
        child.standardInput = stdin
        child.terminationHandler = { [weak self] process in
            DispatchQueue.main.async {
                self?.finishTask(status: process.terminationStatus)
            }
        }
        stdout.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            if data.isEmpty {
                handle.readabilityHandler = nil
                return
            }
            let text = String(decoding: data, as: UTF8.self)
            DispatchQueue.main.async {
                self?.receive(text)
            }
        }

        do {
            try child.run()
            process = child
            inputPipe = stdin
            isRunning = true
            if operation != "task" {
                mcpLogger.info(
                    "Python MCP backend launched operation=\(operation, privacy: .public) pid=\(child.processIdentifier)"
                )
            }
        } catch {
            if operation != "task" {
                mcpLogger.error(
                    "Python MCP backend launch failed operation=\(operation, privacy: .public) error=\(error.localizedDescription, privacy: .public)"
                )
            }
            errorMessage = "Could not start the assistant: \(error.localizedDescription)"
            autonomy = .approval
        }
    }

    func approveAction() {
        respondToAction("y\n")
    }

    func allowAllForTask() {
        respondToAction("all\n")
    }

    func rejectAction() {
        respondToAction("n\n")
        stopTask()
    }

    func stopTask() {
        guard let process, process.isRunning else { return }
        pendingApproval = nil
        output += "\nStopped by you.\n"
        process.terminate()
    }

    private var projectRoot: URL {
        if let configuredRoot = ProcessInfo.processInfo.environment["ASSISTANT_PROJECT_DIR"] {
            return URL(fileURLWithPath: configuredRoot, isDirectory: true)
        }
        return URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
    }

    private func receive(_ text: String) {
        outputBuffer += text
        let lines = outputBuffer.components(separatedBy: .newlines)
        outputBuffer = lines.last ?? ""
        for line in lines.dropLast() {
            if line.hasPrefix("APPROVAL_REQUIRED: ") {
                pendingApproval = String(line.dropFirst("APPROVAL_REQUIRED: ".count))
            } else if line.hasPrefix("SCREEN_ACCESS_DENIED:") {
                hasScreenCaptureAccess = false
                output += String(line.dropFirst("SCREEN_ACCESS_DENIED:".count)).trimmingCharacters(in: .whitespaces) + "\n"
            } else if line.hasPrefix("SCREEN_CAPTURE_OK:") {
                hasScreenCaptureAccess = true
                output += "Screen capture succeeded (\(line.dropFirst("SCREEN_CAPTURE_OK:".count))).\n"
            } else if line.hasPrefix("CHAT_REPLY_JSON:") {
                receiveChatReply(String(line.dropFirst("CHAT_REPLY_JSON:".count)))
            } else if line.hasPrefix("MCP_RESULT_JSON:") {
                receiveMCPResult(String(line.dropFirst("MCP_RESULT_JSON:".count)))
            } else if line.hasPrefix("MCP_LOG_FILE:") {
                let path = String(line.dropFirst("MCP_LOG_FILE:".count))
                mcpLogger.info("Python MCP log file=\(path, privacy: .public)")
                output += "MCP diagnostics: \(path)\n"
            } else if line.hasPrefix("MCP error:") {
                mcpLogger.error(
                    "Python MCP backend error=\(String(line.dropFirst("MCP error:".count)), privacy: .public)"
                )
                output += line + "\n"
            } else {
                output += line + "\n"
            }
        }
    }

    private func receiveChatReply(_ encodedReply: String) {
        guard let data = encodedReply.data(using: .utf8),
              let reply = try? JSONDecoder().decode(String.self, from: data) else {
            output += "Received an invalid chat response.\n"
            return
        }
        output += reply + "\n"
        speechSynthesizer.speak(AVSpeechUtterance(string: reply))
    }

    private func receiveMCPResult(_ json: String) {
        guard let data = json.data(using: .utf8) else {
            mcpLogger.error("Python MCP backend returned invalid UTF-8 JSON")
            output += "MCP returned invalid JSON.\n"
            return
        }
        if let response = try? JSONDecoder().decode(MCPConnectionResponse.self, from: data) {
            mcpTools = response.servers.flatMap { server in
                server.tools.map {
                    MCPTool(
                        name: $0.name,
                        description: $0.description ?? "",
                        inputSchema: $0.inputSchema ?? [:],
                        server: server.name
                    )
                }
            }
            selectedMCPToolID = mcpTools.first(where: { $0.id == selectedMCPToolID })?.id
                ?? mcpTools.first?.id
                ?? ""
            let errorCount = response.errors.count
            mcpStatus = "\(response.servers.count) server(s), \(mcpTools.count) tool(s)"
            mcpLogger.info(
                "MCP tools/list summary servers=\(response.servers.map(\.name), privacy: .public) tools=\(self.mcpTools.map(\.id), privacy: .public) errors=\(errorCount)"
            )
            if errorCount > 0 {
                mcpStatus += "; \(errorCount) server(s) failed"
                for (name, error) in response.errors {
                    mcpLogger.error(
                        "MCP connection error server=\(name, privacy: .public) detail=\(error, privacy: .public)"
                    )
                }
                output += "Connection errors:\n"
                for (name, error) in response.errors.sorted(by: { $0.key < $1.key }) {
                    output += "\(name): \(error)\n"
                }
            }
            if response.servers.isEmpty && errorCount == 0 {
                mcpStatus = "No MCP servers are configured."
            }
            output += "Connected to \(response.servers.count) MCP server(s); found \(mcpTools.count) tool(s).\n"
            return
        }
        guard let value = try? JSONSerialization.jsonObject(with: data),
              let prettyData = try? JSONSerialization.data(
                withJSONObject: value,
                options: [.prettyPrinted, .sortedKeys]
              ),
              let pretty = String(data: prettyData, encoding: .utf8) else {
            mcpLogger.error("Python MCP backend returned a malformed result payload")
            output += json + "\n"
            return
        }
        mcpLogger.info("MCP tool-call result received")
        output += pretty + "\n"
    }

    private func respondToAction(_ response: String) {
        guard let inputPipe, pendingApproval != nil else { return }
        inputPipe.fileHandleForWriting.write(Data(response.utf8))
        pendingApproval = nil
    }

    private func finishTask(status: Int32) {
        if let runningOperation, runningOperation != "task" {
            mcpLogger.info(
                "Python MCP backend exited operation=\(runningOperation, privacy: .public) status=\(status)"
            )
        }
        if !outputBuffer.isEmpty {
            output += outputBuffer + "\n"
            outputBuffer = ""
        }
        if status != 0 && !output.contains("Error:") && !output.contains("MCP error:") {
            errorMessage = "Task ended with exit code \(status). Check Activity for details."
        }
        process = nil
        inputPipe = nil
        runningOperation = nil
        isRunning = false
        pendingApproval = nil
        researchWeb = false
        autonomy = .approval
        voiceStatus = "Task finished. Hold Control+Option to speak again."
    }
}

struct AssistantPanel: View {
    @EnvironmentObject private var controller: TaskController

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                header
                Divider()
                modePicker
                if controller.mode == .control {
                    screenCaptureControls
                }
                if controller.mode == .mcp {
                    mcpConfigurationView
                } else {
                    taskInput
                    if controller.mode == .control {
                        webResearchToggle
                        autonomyPicker
                    }
                }
                voiceControls
                actionControls
                if let pendingApproval = controller.pendingApproval {
                    approvalCard(pendingApproval)
                }
                if let error = controller.errorMessage {
                    Label(error, systemImage: "exclamationmark.triangle")
                        .font(.system(size: 11))
                        .foregroundStyle(.red)
                        .fixedSize(horizontal: false, vertical: true)
                }
                outputView
                footer
            }
            .padding(20)
        }
        .scrollIndicators(.visible)
        .background(Color(nsColor: .windowBackgroundColor))
        .onAppear {
            // Voice is owned by ChatController; a second hotkey tap would double-fire.
            controller.refreshScreenCaptureAccess()
        }
    }

    private var header: some View {
        HStack(spacing: 11) {
            Image(systemName: "waveform")
                .font(.system(size: 16, weight: .semibold))
                .foregroundStyle(Color.accentColor)
                .frame(width: 38, height: 38)
                .background(Color.accentColor.opacity(0.12), in: RoundedRectangle(cornerRadius: 11))
            VStack(alignment: .leading, spacing: 2) {
                Text("Assistant").font(.system(size: 16, weight: .semibold))
                Text("Private, local, and under your control")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            Spacer()
            HStack(spacing: 5) {
                Circle()
                    .fill(controller.isRunning ? Color.orange : Color.green)
                    .frame(width: 6, height: 6)
                Text(controller.isRunning ? "Working" : "On device")
                    .font(.system(size: 10, weight: .medium))
                    .foregroundStyle(.secondary)
            }
            .padding(.horizontal, 9)
            .padding(.vertical, 6)
            .background(.quaternary.opacity(0.6), in: Capsule())
        }
    }

    private var modePicker: some View {
        HStack(spacing: 5) {
            modeButton(.ask, symbol: "bubble.left.and.bubble.right")
            modeButton(.control, symbol: "cursorarrow.click")
            modeButton(.mcp, symbol: "point.3.connected.trianglepath.dotted")
        }
        .padding(4)
        .background(Color(nsColor: .controlBackgroundColor), in: RoundedRectangle(cornerRadius: 11))
    }

    private func modeButton(_ mode: AssistantMode, symbol: String) -> some View {
        let selected = controller.mode == mode
        return Button {
            controller.mode = mode
        } label: {
            Label(mode.title, systemImage: symbol)
                .font(.system(size: 11, weight: .semibold))
                .frame(maxWidth: .infinity)
                .padding(.vertical, 8)
                .foregroundStyle(selected ? Color.primary : Color.secondary)
                .background {
                    if selected {
                        RoundedRectangle(cornerRadius: 8)
                            .fill(Color(nsColor: .windowBackgroundColor))
                            .shadow(color: .black.opacity(0.08), radius: 2, y: 1)
                    }
                }
        }
        .buttonStyle(.plain)
        .disabled(controller.isRunning)
    }

    private var taskInput: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(controller.mode == .ask ? "YOUR QUESTION OR MESSAGE" : "YOUR TASK")
                .font(.system(size: 10, weight: .semibold))
                .tracking(0.8)
                .foregroundStyle(.secondary)
            ZStack(alignment: .topLeading) {
                if controller.task.isEmpty {
                    Text(controller.mode == .ask
                         ? "Ask a question, or tell me what you need…"
                         : "Describe what you want done…")
                        .font(.system(size: 14))
                        .foregroundStyle(.tertiary)
                        .padding(.horizontal, 11)
                        .padding(.vertical, 12)
                        .allowsHitTesting(false)
                }
                TextEditor(text: $controller.task)
                    .font(.system(size: 14))
                    .scrollContentBackground(.hidden)
                    .padding(5)
                    .accessibilityLabel("Task instructions")
            }
            .frame(height: 106)
            .background(Color(nsColor: .textBackgroundColor), in: RoundedRectangle(cornerRadius: 12))
            .overlay {
                RoundedRectangle(cornerRadius: 12)
                    .strokeBorder(Color(nsColor: .separatorColor).opacity(0.55), lineWidth: 1)
            }
            .disabled(controller.isRunning)
        }
    }

    private var screenCaptureControls: some View {
        VStack(alignment: .leading, spacing: 9) {
            Label(
                controller.hasScreenCaptureAccess
                    ? "Screen Recording access is enabled"
                    : "Screen Recording access is not detected",
                systemImage: controller.hasScreenCaptureAccess
                    ? "checkmark.circle.fill"
                    : "exclamationmark.triangle.fill"
            )
            .font(.system(size: 12, weight: .medium))
            .foregroundStyle(controller.hasScreenCaptureAccess ? .green : .orange)

            if !controller.hasScreenCaptureAccess {
                Text("Enable this app in macOS Screen Recording settings. Use Test Capture to verify the Python control process too.")
                    .font(.system(size: 10))
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }

            HStack {
                Button("Request Access") {
                    controller.requestScreenCaptureAccess()
                }
                .controlSize(.small)
                Button("Open System Settings") {
                    controller.openScreenCaptureSettings()
                }
                .controlSize(.small)
                Spacer()
                Button("Refresh") {
                    controller.refreshScreenCaptureAccess()
                }
                .controlSize(.small)
                Button("Test Capture") {
                    controller.testScreenCapture()
                }
                .controlSize(.small)
            }
        }
        .padding(11)
        .background(Color(nsColor: .controlBackgroundColor), in: RoundedRectangle(cornerRadius: 10))
    }

    private var webResearchToggle: some View {
        VStack(alignment: .leading, spacing: 4) {
            Toggle("Search the web for examples", isOn: $controller.researchWeb)
                .font(.system(size: 12, weight: .medium))
                .toggleStyle(.checkbox)
                .disabled(controller.isRunning)
            Text("Your task text is sent to DuckDuckGo. Results appear in Activity.")
                .font(.system(size: 10))
                .foregroundStyle(.secondary)
                .padding(.leading, 21)
        }
    }

    private var autonomyPicker: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Text("HOW SHOULD I PROCEED?")
                    .font(.system(size: 10, weight: .semibold))
                    .tracking(0.8)
                    .foregroundStyle(.secondary)
                Spacer()
                Text("FOR THIS TASK")
                    .font(.system(size: 9, weight: .medium))
                    .tracking(0.5)
                    .foregroundStyle(.tertiary)
            }
            HStack(spacing: 10) {
                autonomyCard(.approval, symbol: "hand.raised")
                autonomyCard(.autonomous, symbol: "forward")
            }
        }
    }

    private func autonomyCard(_ mode: TaskAutonomy, symbol: String) -> some View {
        let selected = controller.autonomy == mode
        return Button {
            controller.autonomy = mode
        } label: {
            HStack(alignment: .top, spacing: 9) {
                Image(systemName: selected ? "largecircle.fill.circle" : "circle")
                    .font(.system(size: 15))
                    .foregroundStyle(selected ? Color.accentColor : Color.secondary)
                    .padding(.top, 1)
                VStack(alignment: .leading, spacing: 4) {
                    Label(mode.title, systemImage: symbol)
                        .font(.system(size: 12, weight: .semibold))
                    Text(mode.detail)
                        .font(.system(size: 10))
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
                Spacer(minLength: 0)
            }
            .padding(11)
            .frame(maxWidth: .infinity, minHeight: 67, alignment: .leading)
            .background(
                selected ? Color.accentColor.opacity(0.08) : Color(nsColor: .controlBackgroundColor),
                in: RoundedRectangle(cornerRadius: 11)
            )
            .overlay {
                RoundedRectangle(cornerRadius: 11)
                    .strokeBorder(
                        selected ? Color.accentColor.opacity(0.55) : Color(nsColor: .separatorColor).opacity(0.45),
                        lineWidth: 1
                    )
            }
            .contentShape(RoundedRectangle(cornerRadius: 11))
        }
        .buttonStyle(.plain)
        .disabled(controller.isRunning)
    }

    private var mcpConfigurationView: some View {
        VStack(alignment: .leading, spacing: 12) {
            VStack(alignment: .leading, spacing: 5) {
                Text("LOCAL MCP SERVERS")
                    .font(.system(size: 10, weight: .semibold))
                    .tracking(0.8)
                    .foregroundStyle(.secondary)
                Text("Servers run as local programs with your account permissions. Only connect servers you trust.")
                    .font(.system(size: 11))
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            TextEditor(text: $controller.mcpConfig)
                .font(.system(size: 11, design: .monospaced))
                .scrollContentBackground(.hidden)
                .padding(8)
                .frame(height: 145)
                .background(Color(nsColor: .textBackgroundColor), in: RoundedRectangle(cornerRadius: 10))
                .overlay {
                    RoundedRectangle(cornerRadius: 10)
                        .strokeBorder(Color(nsColor: .separatorColor).opacity(0.5), lineWidth: 1)
                }
                .disabled(controller.isRunning)
                .accessibilityLabel("MCP server configuration")
            if !controller.mcpTools.isEmpty {
                Picker("Tool", selection: $controller.selectedMCPToolID) {
                    ForEach(controller.mcpTools) { tool in
                        Text("\(tool.server) · \(tool.name)").tag(tool.id)
                    }
                }
                .disabled(controller.isRunning)
                if let tool = selectedMCPTool {
                    Text(tool.description.isEmpty ? "No description provided." : tool.description)
                        .font(.system(size: 11))
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                    Text(mcpToolSchema(tool))
                        .font(.system(size: 10, design: .monospaced))
                        .foregroundStyle(.secondary)
                        .textSelection(.enabled)
                    Text("Arguments (JSON)")
                        .font(.system(size: 10, weight: .semibold))
                        .foregroundStyle(.secondary)
                    TextEditor(text: $controller.mcpArguments)
                        .font(.system(size: 11, design: .monospaced))
                        .scrollContentBackground(.hidden)
                        .padding(6)
                        .frame(height: 75)
                        .background(Color(nsColor: .textBackgroundColor), in: RoundedRectangle(cornerRadius: 9))
                        .overlay {
                            RoundedRectangle(cornerRadius: 9)
                                .strokeBorder(Color(nsColor: .separatorColor).opacity(0.5), lineWidth: 1)
                        }
                        .disabled(controller.isRunning)
                    Button {
                        controller.requestMCPToolCall()
                    } label: {
                        Label("Review & Run Tool", systemImage: "play.fill")
                    }
                    .buttonStyle(.borderedProminent)
                    .disabled(controller.isRunning)
                }
            }
            Text(controller.mcpStatus)
                .font(.system(size: 10))
                .foregroundStyle(.secondary)
            Text("Tool calls require confirmation. MCP servers may access files or services.")
                .font(.system(size: 10))
                .foregroundStyle(.orange)
        }
        .confirmationDialog(
            mcpConfirmationTitle,
            isPresented: $controller.showingMCPConfirmation,
            titleVisibility: .visible
        ) {
            Button("Run this tool", role: .destructive) {
                controller.confirmMCPToolCall()
            }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text(mcpConfirmationMessage)
        }
    }

    private var selectedMCPTool: MCPTool? {
        controller.mcpTools.first { $0.id == controller.selectedMCPToolID }
    }

    private var mcpConfirmationTitle: String {
        guard let tool = selectedMCPTool else { return "Run MCP tool?" }
        return "Run \(tool.server).\(tool.name)?"
    }

    private var mcpConfirmationMessage: String {
        guard let tool = selectedMCPTool else {
            return "This call may change data in the connected service."
        }
        return "\(tool.description)\n\nThis call may change data in the connected service."
    }

    private func mcpToolSchema(_ tool: MCPTool) -> String {
        guard let data = try? JSONEncoder().encode(tool.inputSchema),
              let json = String(data: data, encoding: .utf8) else {
            return "Arguments follow the tool's JSON schema."
        }
        return "Input schema: \(json)"
    }

    private var voiceControls: some View {
        HStack(spacing: 12) {
            Button {
                controller.toggleVoiceCapture()
            } label: {
                Image(systemName: controller.isListening ? "stop.fill" : "mic.fill")
                    .font(.system(size: 12, weight: .semibold))
                    .foregroundStyle(controller.isListening ? .white : Color.accentColor)
                    .frame(width: 34, height: 34)
                    .background(
                        controller.isListening ? Color.red : Color.accentColor.opacity(0.11),
                        in: Circle()
                    )
            }
            .buttonStyle(.plain)
            .help(controller.isListening ? "Stop listening" : "Start voice input")
            .disabled(controller.isRunning)
            VStack(alignment: .leading, spacing: 3) {
                Text(controller.isListening ? "Listening" : "Voice input")
                    .font(.system(size: 12, weight: .medium))
                    .foregroundStyle(controller.isListening ? .red : .primary)
                Text(controller.voiceStatus)
                    .font(.system(size: 10))
                    .foregroundStyle(.secondary)
                    .lineLimit(2)
            }
            Spacer()
            if controller.voiceStatus.localizedCaseInsensitiveContains("permissions") {
                Button("Retry") {
                    controller.retryVoiceShortcut()
                }
                .controlSize(.small)
            }
            Text("⌃ ⌥")
                .font(.system(size: 12, weight: .medium, design: .rounded))
                .foregroundStyle(.secondary)
                .padding(.horizontal, 9)
                .padding(.vertical, 5)
                .background(.quaternary.opacity(0.7), in: RoundedRectangle(cornerRadius: 6))
        }
    }

    private var actionControls: some View {
        HStack {
            if controller.isRunning {
                Label("Task running", systemImage: "circle.fill")
                    .font(.caption)
                    .foregroundStyle(.orange)
                Spacer()
                Button(role: .destructive) {
                    controller.stopTask()
                } label: {
                    Label("Stop", systemImage: "stop.fill")
                }
                .keyboardShortcut(.cancelAction)
            } else if controller.mode == .mcp {
                Spacer()
                Button {
                    controller.connectMCP()
                } label: {
                    Label("Save & Connect", systemImage: "link")
                }
                .buttonStyle(.borderedProminent)
            } else {
                Text(controller.mode == .ask
                     ? "Answers use your local Ollama model."
                     : "Control uses the screen and keyboard.")
                    .font(.system(size: 10))
                    .foregroundStyle(.secondary)
                Spacer(minLength: 0)
                Button {
                    controller.startTask()
                } label: {
                    Label(
                        controller.mode == .ask ? "Ask" : "Start task",
                        systemImage: controller.mode == .ask ? "arrow.up.circle.fill" : "play.fill"
                    )
                }
                .buttonStyle(.borderedProminent)
                .disabled(controller.task.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                .keyboardShortcut(.return, modifiers: [.command])
            }
        }
    }

    private func approvalCard(_ action: String) -> some View {
        VStack(alignment: .leading, spacing: 11) {
            Label("Action needs your approval", systemImage: "hand.raised.fill")
                .font(.system(size: 13, weight: .semibold))
                .foregroundStyle(.orange)
            Text(action)
                .font(.system(size: 12))
                .textSelection(.enabled)
            HStack {
                Button("Stop task", role: .cancel) {
                    controller.rejectAction()
                }
                .buttonStyle(.bordered)
                Button("Allow All") {
                    controller.allowAllForTask()
                }
                .buttonStyle(.bordered)
                .help("Approve this action and all remaining actions in this task.")
                Spacer(minLength: 0)
                Button("Allow") {
                    controller.approveAction()
                }
                .buttonStyle(.borderedProminent)
            }
            Text("Allow All applies only until this task ends.")
                .font(.system(size: 10))
                .foregroundStyle(.secondary)
        }
        .padding(13)
        .background(Color.orange.opacity(0.08), in: RoundedRectangle(cornerRadius: 12))
        .overlay {
            RoundedRectangle(cornerRadius: 12)
                .strokeBorder(Color.orange.opacity(0.23), lineWidth: 1)
        }
    }

    private var outputView: some View {
        VStack(alignment: .leading, spacing: 7) {
            HStack {
                Text("ACTIVITY")
                    .font(.system(size: 10, weight: .semibold))
                    .tracking(0.8)
                    .foregroundStyle(.secondary)
                Spacer()
                if controller.isRunning {
                    ProgressView()
                        .controlSize(.small)
                }
            }
            ScrollView {
                Group {
                    if controller.output.isEmpty {
                        Text("Your task progress will appear here.")
                            .foregroundStyle(.tertiary)
                    } else {
                        Text(controller.output)
                            .foregroundStyle(.secondary)
                    }
                }
                .font(.system(size: 11))
                .frame(maxWidth: .infinity, alignment: .leading)
                .textSelection(.enabled)
                .padding(11)
            }
            .frame(minHeight: 80, maxHeight: 128)
            .background(Color(nsColor: .controlBackgroundColor), in: RoundedRectangle(cornerRadius: 10))
            .overlay {
                RoundedRectangle(cornerRadius: 10)
                    .strokeBorder(Color(nsColor: .separatorColor).opacity(0.4), lineWidth: 1)
            }
        }
    }

    private var footer: some View {
        HStack(spacing: 8) {
            Image(systemName: "lock.shield")
                .font(.system(size: 10))
                .foregroundStyle(.secondary)
            Text("Screen images go to Ollama running on this Mac.")
                .font(.system(size: 10))
                .foregroundStyle(.secondary)
            Spacer(minLength: 0)
            Button {
                NSApplication.shared.terminate(nil)
            } label: {
                Image(systemName: "power")
            }
            .buttonStyle(.plain)
            .foregroundStyle(.secondary)
            .help("Quit Assistant")
        }
    }
}
