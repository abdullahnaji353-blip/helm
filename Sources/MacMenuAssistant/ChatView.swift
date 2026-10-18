import AVFoundation
import AppKit
import Carbon.HIToolbox
import ApplicationServices
import Security
import Speech
import SwiftUI
import UserNotifications

private let jarvisCyan = Color(red: 0.30, green: 0.85, blue: 1.0)
private let jarvisBlue = Color(red: 0.16, green: 0.42, blue: 1.0)

/// System-wide hotkey through Carbon: works from any app and needs no permission.
final class GlobalHotKey {
    private var hotKeyRef: EventHotKeyRef?
    private var handlerRef: EventHandlerRef?
    private let hotKeyID: EventHotKeyID
    fileprivate let action: () -> Void
    private(set) var registrationError: OSStatus?

    init(keyCode: UInt32, modifiers: UInt32, id: UInt32, action: @escaping () -> Void) {
        self.action = action
        hotKeyID = EventHotKeyID(signature: OSType(0x4348_4254), id: id)
        var spec = EventTypeSpec(eventClass: OSType(kEventClassKeyboard), eventKind: UInt32(kEventHotKeyPressed))
        let handlerStatus = InstallEventHandler(GetApplicationEventTarget(), { _, event, userData in
            guard let userData else { return noErr }
            var receivedID = EventHotKeyID()
            let status = GetEventParameter(
                event,
                EventParamName(kEventParamDirectObject),
                EventParamType(typeEventHotKeyID),
                nil,
                MemoryLayout<EventHotKeyID>.size,
                nil,
                &receivedID
            )
            guard status == noErr else { return status }
            let hotKey = Unmanaged<GlobalHotKey>.fromOpaque(userData).takeUnretainedValue()
            guard receivedID.signature == hotKey.hotKeyID.signature,
                  receivedID.id == hotKey.hotKeyID.id else { return noErr }
            DispatchQueue.main.async { hotKey.action() }
            return noErr
        }, 1, &spec, Unmanaged.passUnretained(self).toOpaque(), &handlerRef)
        guard handlerStatus == noErr else {
            registrationError = handlerStatus
            return
        }
        let registrationStatus = RegisterEventHotKey(
            keyCode, modifiers, hotKeyID, GetApplicationEventTarget(), 0, &hotKeyRef
        )
        if registrationStatus != noErr {
            registrationError = registrationStatus
            if let handlerRef {
                RemoveEventHandler(handlerRef)
                self.handlerRef = nil
            }
        }
    }

    deinit {
        if let hotKeyRef { UnregisterEventHotKey(hotKeyRef) }
        if let handlerRef { RemoveEventHandler(handlerRef) }
    }
}

/// Mirrors PROVIDERS in providers.py.
struct ProviderOption: Identifiable {
    let id: String
    let label: String
    let symbol: String
    let defaultModel: String
    let keyURL: String?

    static let all = [
        ProviderOption(id: "ollama", label: "Local (Ollama)", symbol: "lock.laptopcomputer", defaultModel: "gemma4:e2b", keyURL: nil),
        ProviderOption(id: "anthropic", label: "Claude", symbol: "sparkle", defaultModel: "claude-opus-5-5", keyURL: "https://console.anthropic.com/settings/keys"),
        ProviderOption(id: "openai", label: "ChatGPT", symbol: "bubble.left.and.text.bubble.right", defaultModel: "gpt-5-mini", keyURL: "https://platform.openai.com/api-keys"),
        ProviderOption(id: "gemini", label: "Gemini", symbol: "diamond", defaultModel: "gemini-2.5-flash", keyURL: "https://aistudio.google.com/apikey"),
        ProviderOption(id: "openrouter", label: "OpenRouter", symbol: "arrow.triangle.branch", defaultModel: "openai/gpt-5-mini", keyURL: "https://openrouter.ai/keys"),
    ]

    static func find(_ id: String) -> ProviderOption { all.first { $0.id == id } ?? all[0] }
}

/// API keys live in the login Keychain, never in files; the backend gets one via env.
enum KeyStore {
    private static let service = "Chatbot API Keys"

    private static func query(_ provider: String) -> [String: Any] {
        [kSecClass as String: kSecClassGenericPassword,
         kSecAttrService as String: service,
         kSecAttrAccount as String: provider]
    }

    static func save(_ key: String, for provider: String) -> Bool {
        delete(provider)
        var item = query(provider)
        item[kSecValueData as String] = Data(key.utf8)
        return SecItemAdd(item as CFDictionary, nil) == errSecSuccess
    }

    static func read(_ provider: String) -> String? {
        var request = query(provider)
        request[kSecReturnData as String] = true
        request[kSecMatchLimit as String] = kSecMatchLimitOne
        var result: CFTypeRef?
        guard SecItemCopyMatching(request as CFDictionary, &result) == errSecSuccess,
              let data = result as? Data else { return nil }
        return String(data: data, encoding: .utf8)
    }

    /// Whether a key is saved, without reading the secret (so macOS shows no access prompt).
    static func exists(_ provider: String) -> Bool {
        var request = query(provider)
        request[kSecReturnAttributes as String] = true
        request[kSecMatchLimit as String] = kSecMatchLimitOne
        var result: CFTypeRef?
        return SecItemCopyMatching(request as CFDictionary, &result) == errSecSuccess
    }

    static func delete(_ provider: String) {
        SecItemDelete(query(provider) as CFDictionary)
    }

    static func saveSecret(_ value: String, key: String) -> Bool {
        delete(key)
        var item = query(key)
        item[kSecValueData as String] = Data(value.utf8)
        return SecItemAdd(item as CFDictionary, nil) == errSecSuccess
    }

    static func readSecret(_ key: String) -> String? {
        read(key)
    }

    static func hasSecret(_ key: String) -> Bool {
        exists(key)
    }

    static func deleteSecret(_ key: String) {
        delete(key)
    }
}

struct ChatLine: Identifiable, Equatable {
    enum Role { case user, bot, activity, error }
    let id = UUID()
    let role: Role
    var text: String
}

/// Drives the Helm conversation through `assistant.py --chat`, which keeps
/// history and calls device tools (apps, URLs, volume, status, screen control).
@MainActor
final class ChatController: NSObject, ObservableObject {
    @Published var lines: [ChatLine] = []
    @Published var draft = ""
    @Published var isThinking = false
    @Published var isListening = false
    @Published var dictating = false
    private var dictationBase = ""
    @Published var voiceStatus = "Hold ⌃⌥ to talk"
    @Published var emergencyStopShortcutAvailable = false
    @Published var pendingApproval: String?
    @Published var askBeforeActing = UserDefaults.standard.object(forKey: "askBeforeActing") as? Bool ?? true {
        didSet { UserDefaults.standard.set(askBeforeActing, forKey: "askBeforeActing") }
    }
    @Published var speakReplies = UserDefaults.standard.object(forKey: "speakReplies") as? Bool ?? true {
        didSet { UserDefaults.standard.set(speakReplies, forKey: "speakReplies") }
    }
    @Published var provider = UserDefaults.standard.string(forKey: "provider") ?? "ollama" {
        didSet { UserDefaults.standard.set(provider, forKey: "provider") }
    }
    /// Chat tools the user switched off (names match CHAT_TOOLS in assistant.py).
    @Published var disabledTools = Set(UserDefaults.standard.stringArray(forKey: "disabledTools") ?? []) {
        didSet { UserDefaults.standard.set(Array(disabledTools), forKey: "disabledTools") }
    }
    @Published var browser = UserDefaults.standard.string(forKey: "browser")
        ?? (FileManager.default.fileExists(atPath: "/Applications/Google Chrome.app") ? "chrome" : "safari") {
        didSet { UserDefaults.standard.set(browser, forKey: "browser") }
    }
    @Published var voiceHotkey = UserDefaults.standard.object(forKey: "voiceHotkey") as? Bool ?? true {
        didSet { UserDefaults.standard.set(voiceHotkey, forKey: "voiceHotkey") }
    }
    @Published var keyCheck: (ok: Bool, message: String)?
    @Published var isCheckingKey = false
    @Published var cloudVision = UserDefaults.standard.bool(forKey: "cloudVision") {
        didSet { UserDefaults.standard.set(cloudVision, forKey: "cloudVision") }
    }
    @Published var onboarded = !ChatController.isFirstRun() {
        didSet { UserDefaults.standard.set(onboarded, forKey: "onboarded") }
    }

    private static var supportDirectory: URL {
        FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Application Support/Local Desktop Assistant", isDirectory: true)
    }

    /// First run only if nothing of Helm exists on this Mac yet: no setup flag, no
    /// setup marker (survives preference resets), no saved keys and no chat history.
    static func isFirstRun() -> Bool {
        let defaults = UserDefaults.standard
        if defaults.bool(forKey: "onboarded") { return false }
        let returning = FileManager.default.fileExists(atPath: supportDirectory.appendingPathComponent("setup-complete.json").path)
            || FileManager.default.fileExists(atPath: supportDirectory.appendingPathComponent("chat_history.json").path)
            || ProviderOption.all.contains { KeyStore.exists($0.id) }
        if returning { defaults.set(true, forKey: "onboarded") }
        return !returning
    }

    func completeSetup() {
        let info: [String: Any] = [
            "completedAt": ISO8601DateFormatter().string(from: Date()),
            "version": Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "dev",
        ]
        try? FileManager.default.createDirectory(at: Self.supportDirectory, withIntermediateDirectories: true)
        if let data = try? JSONSerialization.data(withJSONObject: info) {
            try? data.write(to: Self.supportDirectory.appendingPathComponent("setup-complete.json"))
        }
        onboarded = true
        configureVoice()
        let greeting = "All systems online. How can I help?"
        lines.append(ChatLine(role: .bot, text: greeting))
        speak(greeting)
    }

    func isEnabled(_ tools: [String]) -> Bool { !tools.contains { disabledTools.contains($0) } }

    func setEnabled(_ tools: [String], _ enabled: Bool) {
        if enabled { disabledTools.subtract(tools) } else { disabledTools.formUnion(tools) }
    }

    /// Runs `assistant.py --check-key` for the selected brain.
    func checkKey() {
        isCheckingKey = true
        keyCheck = nil
        var arguments = ["--check-key", "--provider", provider]
        if !model(for: provider).isEmpty { arguments += ["--model", model(for: provider)] }
        let child = Process()
        let output = Pipe()
        child.executableURL = projectRoot.appendingPathComponent(".venv/bin/python")
        child.arguments = ["-u", projectRoot.appendingPathComponent("assistant.py").path] + arguments
        child.currentDirectoryURL = projectRoot
        child.standardOutput = output
        child.standardError = output
        var environment = ProcessInfo.processInfo.environment
        if provider != "ollama", let key = KeyStore.read(provider) { environment["HELM_API_KEY"] = key }
        child.environment = environment
        child.terminationHandler = { [weak self] _ in
            let text = String(decoding: output.fileHandleForReading.readDataToEndOfFile(), as: UTF8.self)
            let line = text.components(separatedBy: .newlines).last { $0.hasPrefix("KEY_OK:") || $0.hasPrefix("KEY_ERROR:") }
            DispatchQueue.main.async {
                self?.isCheckingKey = false
                if let line, line.hasPrefix("KEY_OK:") {
                    self?.keyCheck = (true, String(line.dropFirst("KEY_OK:".count)))
                } else {
                    self?.keyCheck = (false, line.map { String($0.dropFirst("KEY_ERROR:".count)) } ?? "The check failed. See Console for details.")
                }
            }
        }
        do { try child.run() } catch {
            isCheckingKey = false
            keyCheck = (false, "Could not start the check: \(error.localizedDescription)")
        }
    }
    @Published var showProviderSettings = false
    @Published var whatsappBridgeStatus = "Not configured"
    @Published var whatsappNotificationError: String?
    private var whatsappBridgeProcess: Process?
    private var whatsappTerminationObserver: NSObjectProtocol?
    @Published var telegramBridgeStatus = "Not configured"
    private var telegramBridgeProcess: Process?
    private var telegramTerminationObserver: NSObjectProtocol?
    @Published var discordBridgeStatus = "Not configured"
    private var discordBridgeProcess: Process?
    private var discordTerminationObserver: NSObjectProtocol?
    @Published var slackBridgeStatus = "Not configured"
    private var slackBridgeProcess: Process?
    private var slackTerminationObserver: NSObjectProtocol?
    /// Bumped by the Control+Shift+§ shortcut; the menu bar label opens the HUD when it changes.
    @Published var hudRequests = 0
    /// Three background agents, each with its own engine, conversation and coloured cursor.
    lazy var agents: [AgentRun] = (1...3).map { AgentRun(number: $0, owner: self) }

    /// Starts `task` on the first idle agent; false if all three are busy.
    @discardableResult
    func dispatch(_ task: String) -> Bool {
        let text = task.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty, let agent = agents.first(where: { !$0.isRunning }) else { return false }
        agent.run(text)
        objectWillChange.send()
        return true
    }

    func dispatchDraft() {
        if dispatch(draft) {
            AgentCursor.shared.say("Sent to an agent: \(draft)")
            draft = ""
        } else {
            lines.append(ChatLine(role: .error, text: "All three agents are busy. Stop one or wait."))
        }
    }

    /// The same engine settings the main chat uses.
    func engineArguments(for text: String, history: URL? = nil) -> [String] {
        var arguments = ["--chat", text, "--autonomy", askBeforeActing ? "approval" : "autonomous", "--provider", provider]
        if !model(for: provider).isEmpty { arguments += ["--model", model(for: provider)] }
        if !disabledTools.isEmpty { arguments += ["--disable-tools", disabledTools.sorted().joined(separator: ",")] }
        if cloudVision { arguments.append("--cloud-vision") }
        arguments += ["--browser", browser]
        if let history { arguments += ["--history", history.path] }
        return arguments
    }

    func engineEnvironment() -> [String: String] {
        var environment = ProcessInfo.processInfo.environment
        if provider != "ollama", let key = KeyStore.read(provider) { environment["HELM_API_KEY"] = key }
        return environment
    }

    func startWhatsAppBridge() {
        guard whatsappBridgeProcess?.isRunning != true else {
            whatsappBridgeStatus = "Running"
            return
        }
        guard let accessToken = KeyStore.readSecret("whatsapp.access-token"),
              let appSecret = KeyStore.readSecret("whatsapp.app-secret"),
              let verifyToken = KeyStore.readSecret("whatsapp.verify-token") else {
            whatsappBridgeStatus = "Save the three WhatsApp secrets first."
            return
        }
        let phoneNumberID = UserDefaults.standard.string(forKey: "whatsapp.phone-number-id") ?? ""
        let ownerNumber = UserDefaults.standard.string(forKey: "whatsapp.owner-number") ?? ""
        guard !phoneNumberID.isEmpty, !ownerNumber.isEmpty else {
            whatsappBridgeStatus = "Enter the WhatsApp Phone Number ID and your number."
            return
        }
        let python = projectRoot.appendingPathComponent(".venv/bin/python")
        let script = projectRoot.appendingPathComponent("whatsapp_bridge.py")
        guard FileManager.default.isExecutableFile(atPath: python.path),
              FileManager.default.fileExists(atPath: script.path) else {
            whatsappBridgeStatus = "Project Python or whatsapp_bridge.py is missing."
            return
        }

        let child = Process()
        let output = Pipe()
        child.executableURL = python
        child.arguments = ["-u", script.path]
        child.currentDirectoryURL = projectRoot
        child.standardOutput = output
        child.standardError = output
        var environment = ProcessInfo.processInfo.environment
        environment["ASSISTANT_PROJECT_DIR"] = projectRoot.path
        environment["HELM_WHATSAPP_ACCESS_TOKEN"] = accessToken
        environment["HELM_WHATSAPP_PHONE_NUMBER_ID"] = phoneNumberID
        environment["HELM_WHATSAPP_APP_SECRET"] = appSecret
        environment["HELM_WHATSAPP_VERIFY_TOKEN"] = verifyToken
        environment["HELM_WHATSAPP_OWNER_NUMBER"] = ownerNumber
        environment["HELM_WHATSAPP_API_VERSION"] =
            UserDefaults.standard.string(forKey: "whatsapp.api-version") ?? "v23.0"
        environment["HELM_WHATSAPP_PYTHON"] = python.path
        environment["HELM_WHATSAPP_PROVIDER"] = provider
        if !model(for: provider).isEmpty {
            environment["HELM_WHATSAPP_MODEL"] = model(for: provider)
        }
        if provider != "ollama", let key = KeyStore.read(provider) {
            environment["HELM_API_KEY"] = key
        }
        environment["HELM_WHATSAPP_DISABLED_TOOLS"] = disabledTools.sorted().joined(separator: ",")
        child.environment = environment
        child.terminationHandler = { [weak self] process in
            DispatchQueue.main.async {
                guard self?.whatsappBridgeProcess === process else { return }
                self?.whatsappBridgeProcess = nil
                if process.terminationStatus == 0 {
                    self?.whatsappBridgeStatus = "Stopped"
                } else {
                    self?.whatsappBridgeStatus = "Bridge exited (code \(process.terminationStatus)). Check its log."
                    self?.notifyBridgeStopped(platform: "WhatsApp", code: process.terminationStatus)
                }
            }
        }
        output.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            if data.isEmpty {
                handle.readabilityHandler = nil
                return
            }
            let text = String(decoding: data, as: UTF8.self)
            DispatchQueue.main.async {
                if text.contains("WHATSAPP_BRIDGE_READY:") {
                    self?.whatsappBridgeStatus = "Running on 127.0.0.1:8765"
                } else if let line = text.components(separatedBy: .newlines)
                    .first(where: { $0.hasPrefix("WHATSAPP_BRIDGE_ERROR:") }) {
                    self?.whatsappBridgeStatus = String(line.dropFirst("WHATSAPP_BRIDGE_ERROR:".count))
                }
            }
        }
        do {
            try child.run()
            whatsappBridgeProcess = child
            whatsappBridgeStatus = "Starting…"
            if whatsappTerminationObserver == nil {
                whatsappTerminationObserver = NotificationCenter.default.addObserver(
                    forName: NSApplication.willTerminateNotification,
                    object: nil,
                    queue: .main
                ) { [weak self] _ in
                    self?.stopWhatsAppBridge()
                }
            }
        } catch {
            whatsappBridgeStatus = "Could not start bridge: \(error.localizedDescription)"
        }
    }

    func stopWhatsAppBridge() {
        guard let process = whatsappBridgeProcess, process.isRunning else {
            whatsappBridgeStatus = "Stopped"
            return
        }
        process.terminate()
        whatsappBridgeStatus = "Stopping…"
    }

    func startTelegramBridge() {
        guard telegramBridgeProcess?.isRunning != true else {
            telegramBridgeStatus = "Running"
            return
        }
        guard let botToken = KeyStore.readSecret("telegram.bot-token") else {
            telegramBridgeStatus = "Save the Telegram bot token first."
            return
        }
        let python = projectRoot.appendingPathComponent(".venv/bin/python")
        let script = projectRoot.appendingPathComponent("telegram_bridge.py")
        guard FileManager.default.isExecutableFile(atPath: python.path),
              FileManager.default.fileExists(atPath: script.path) else {
            telegramBridgeStatus = "Project Python or telegram_bridge.py is missing."
            return
        }

        let child = Process()
        let output = Pipe()
        child.executableURL = python
        child.arguments = ["-u", script.path]
        child.currentDirectoryURL = projectRoot
        child.standardOutput = output
        child.standardError = output
        var environment = ProcessInfo.processInfo.environment
        environment["ASSISTANT_PROJECT_DIR"] = projectRoot.path
        environment["HELM_TELEGRAM_BOT_TOKEN"] = botToken
        environment["HELM_TELEGRAM_OWNER_CHAT_ID"] =
            UserDefaults.standard.string(forKey: "telegram.owner-chat-id") ?? ""
        if let proxy = KeyStore.readSecret("telegram.proxy-url"), !proxy.isEmpty {
            environment["HELM_TELEGRAM_PROXY_URL"] = proxy
        }
        environment["HELM_TELEGRAM_PYTHON"] = python.path
        environment["HELM_TELEGRAM_PROVIDER"] = provider
        if !model(for: provider).isEmpty {
            environment["HELM_TELEGRAM_MODEL"] = model(for: provider)
        }
        if provider != "ollama", let key = KeyStore.read(provider) {
            environment["HELM_API_KEY"] = key
        }
        environment["HELM_TELEGRAM_DISABLED_TOOLS"] = disabledTools.sorted().joined(separator: ",")
        child.environment = environment
        child.terminationHandler = { [weak self] process in
            DispatchQueue.main.async {
                guard self?.telegramBridgeProcess === process else { return }
                self?.telegramBridgeProcess = nil
                if process.terminationStatus == 0 {
                    self?.telegramBridgeStatus = "Stopped"
                } else {
                    self?.telegramBridgeStatus = "Bridge exited (code \(process.terminationStatus)). Check its log."
                    self?.notifyBridgeStopped(platform: "Telegram", code: process.terminationStatus)
                }
            }
        }
        output.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            if data.isEmpty {
                handle.readabilityHandler = nil
                return
            }
            let text = String(decoding: data, as: UTF8.self)
            DispatchQueue.main.async {
                if let line = text.components(separatedBy: .newlines)
                    .first(where: { $0.hasPrefix("TELEGRAM_BRIDGE_READY:") }) {
                    self?.telegramBridgeStatus = "Running — " + line.dropFirst("TELEGRAM_BRIDGE_READY:".count)
                } else if let line = text.components(separatedBy: .newlines)
                    .first(where: { $0.hasPrefix("TELEGRAM_BRIDGE_ERROR:") }) {
                    self?.telegramBridgeStatus = String(line.dropFirst("TELEGRAM_BRIDGE_ERROR:".count))
                }
            }
        }
        do {
            try child.run()
            telegramBridgeProcess = child
            telegramBridgeStatus = "Starting…"
            if telegramTerminationObserver == nil {
                telegramTerminationObserver = NotificationCenter.default.addObserver(
                    forName: NSApplication.willTerminateNotification,
                    object: nil,
                    queue: .main
                ) { [weak self] _ in
                    self?.stopTelegramBridge()
                }
            }
        } catch {
            telegramBridgeStatus = "Could not start bridge: \(error.localizedDescription)"
        }
    }

    func stopTelegramBridge() {
        guard let process = telegramBridgeProcess, process.isRunning else {
            telegramBridgeStatus = "Stopped"
            return
        }
        process.terminate()
        telegramBridgeStatus = "Stopping…"
    }

    func startDiscordBridge() {
        guard discordBridgeProcess?.isRunning != true else {
            discordBridgeStatus = "Running"
            return
        }
        guard let botToken = KeyStore.readSecret("discord.bot-token") else {
            discordBridgeStatus = "Save the Discord bot token first."
            return
        }
        let channelID = UserDefaults.standard.string(forKey: "discord.channel-id") ?? ""
        let ownerUserID = UserDefaults.standard.string(forKey: "discord.owner-user-id") ?? ""
        guard !channelID.isEmpty else {
            discordBridgeStatus = "Fill in the channel ID first."
            return
        }
        let python = projectRoot.appendingPathComponent(".venv/bin/python")
        let script = projectRoot.appendingPathComponent("discord_bridge.py")
        guard FileManager.default.isExecutableFile(atPath: python.path),
              FileManager.default.fileExists(atPath: script.path) else {
            discordBridgeStatus = "Project Python or discord_bridge.py is missing."
            return
        }

        let child = Process()
        let output = Pipe()
        child.executableURL = python
        child.arguments = ["-u", script.path]
        child.currentDirectoryURL = projectRoot
        child.standardOutput = output
        child.standardError = output
        var environment = ProcessInfo.processInfo.environment
        environment["ASSISTANT_PROJECT_DIR"] = projectRoot.path
        environment["HELM_DISCORD_BOT_TOKEN"] = botToken
        environment["HELM_DISCORD_CHANNEL_ID"] = channelID
        environment["HELM_DISCORD_OWNER_USER_ID"] = ownerUserID
        environment["HELM_DISCORD_PYTHON"] = python.path
        environment["HELM_DISCORD_PROVIDER"] = provider
        if !model(for: provider).isEmpty {
            environment["HELM_DISCORD_MODEL"] = model(for: provider)
        }
        if provider != "ollama", let key = KeyStore.read(provider) {
            environment["HELM_API_KEY"] = key
        }
        environment["HELM_DISCORD_DISABLED_TOOLS"] = disabledTools.sorted().joined(separator: ",")
        child.environment = environment
        child.terminationHandler = { [weak self] process in
            DispatchQueue.main.async {
                guard self?.discordBridgeProcess === process else { return }
                self?.discordBridgeProcess = nil
                if process.terminationStatus == 0 {
                    self?.discordBridgeStatus = "Stopped"
                } else {
                    self?.discordBridgeStatus = "Bridge exited (code \(process.terminationStatus)). Check its log."
                    self?.notifyBridgeStopped(platform: "Discord", code: process.terminationStatus)
                }
            }
        }
        output.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            if data.isEmpty {
                handle.readabilityHandler = nil
                return
            }
            let text = String(decoding: data, as: UTF8.self)
            DispatchQueue.main.async {
                let lines = text.components(separatedBy: .newlines)
                if let line = lines.first(where: { $0.hasPrefix("DISCORD_BRIDGE_READY:") }) {
                    self?.discordBridgeStatus = "Running — " + line.dropFirst("DISCORD_BRIDGE_READY:".count)
                } else if let line = lines.first(where: { $0.hasPrefix("DISCORD_BRIDGE_ERROR:") }) {
                    self?.discordBridgeStatus = String(line.dropFirst("DISCORD_BRIDGE_ERROR:".count))
                }
                if let line = lines.first(where: { $0.hasPrefix("DISCORD_BRIDGE_PAIRED:") }) {
                    UserDefaults.standard.set(
                        String(line.dropFirst("DISCORD_BRIDGE_PAIRED:".count)), forKey: "discord.owner-user-id"
                    )
                }
            }
        }
        do {
            try child.run()
            discordBridgeProcess = child
            discordBridgeStatus = "Starting…"
            if discordTerminationObserver == nil {
                discordTerminationObserver = NotificationCenter.default.addObserver(
                    forName: NSApplication.willTerminateNotification,
                    object: nil,
                    queue: .main
                ) { [weak self] _ in
                    self?.stopDiscordBridge()
                }
            }
        } catch {
            discordBridgeStatus = "Could not start bridge: \(error.localizedDescription)"
        }
    }

    func stopDiscordBridge() {
        guard let process = discordBridgeProcess, process.isRunning else {
            discordBridgeStatus = "Stopped"
            return
        }
        process.terminate()
        discordBridgeStatus = "Stopping…"
    }

    func startSlackBridge() {
        guard slackBridgeProcess?.isRunning != true else {
            slackBridgeStatus = "Running"
            return
        }
        guard let botToken = KeyStore.readSecret("slack.bot-token") else {
            slackBridgeStatus = "Save the Slack bot token first."
            return
        }
        let channelID = UserDefaults.standard.string(forKey: "slack.channel-id") ?? ""
        let ownerUserID = UserDefaults.standard.string(forKey: "slack.owner-user-id") ?? ""
        guard !channelID.isEmpty else {
            slackBridgeStatus = "Fill in the channel ID first."
            return
        }
        let python = projectRoot.appendingPathComponent(".venv/bin/python")
        let script = projectRoot.appendingPathComponent("slack_bridge.py")
        guard FileManager.default.isExecutableFile(atPath: python.path),
              FileManager.default.fileExists(atPath: script.path) else {
            slackBridgeStatus = "Project Python or slack_bridge.py is missing."
            return
        }

        let child = Process()
        let output = Pipe()
        child.executableURL = python
        child.arguments = ["-u", script.path]
        child.currentDirectoryURL = projectRoot
        child.standardOutput = output
        child.standardError = output
        var environment = ProcessInfo.processInfo.environment
        environment["ASSISTANT_PROJECT_DIR"] = projectRoot.path
        environment["HELM_SLACK_BOT_TOKEN"] = botToken
        environment["HELM_SLACK_CHANNEL_ID"] = channelID
        environment["HELM_SLACK_OWNER_USER_ID"] = ownerUserID
        environment["HELM_SLACK_PYTHON"] = python.path
        environment["HELM_SLACK_PROVIDER"] = provider
        if !model(for: provider).isEmpty {
            environment["HELM_SLACK_MODEL"] = model(for: provider)
        }
        if provider != "ollama", let key = KeyStore.read(provider) {
            environment["HELM_API_KEY"] = key
        }
        environment["HELM_SLACK_DISABLED_TOOLS"] = disabledTools.sorted().joined(separator: ",")
        child.environment = environment
        child.terminationHandler = { [weak self] process in
            DispatchQueue.main.async {
                guard self?.slackBridgeProcess === process else { return }
                self?.slackBridgeProcess = nil
                if process.terminationStatus == 0 {
                    self?.slackBridgeStatus = "Stopped"
                } else {
                    self?.slackBridgeStatus = "Bridge exited (code \(process.terminationStatus)). Check its log."
                    self?.notifyBridgeStopped(platform: "Slack", code: process.terminationStatus)
                }
            }
        }
        output.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            if data.isEmpty {
                handle.readabilityHandler = nil
                return
            }
            let text = String(decoding: data, as: UTF8.self)
            DispatchQueue.main.async {
                let lines = text.components(separatedBy: .newlines)
                if let line = lines.first(where: { $0.hasPrefix("SLACK_BRIDGE_READY:") }) {
                    self?.slackBridgeStatus = "Running — " + line.dropFirst("SLACK_BRIDGE_READY:".count)
                } else if let line = lines.first(where: { $0.hasPrefix("SLACK_BRIDGE_ERROR:") }) {
                    self?.slackBridgeStatus = String(line.dropFirst("SLACK_BRIDGE_ERROR:".count))
                }
                if let line = lines.first(where: { $0.hasPrefix("SLACK_BRIDGE_PAIRED:") }) {
                    UserDefaults.standard.set(
                        String(line.dropFirst("SLACK_BRIDGE_PAIRED:".count)), forKey: "slack.owner-user-id"
                    )
                }
            }
        }
        do {
            try child.run()
            slackBridgeProcess = child
            slackBridgeStatus = "Starting…"
            if slackTerminationObserver == nil {
                slackTerminationObserver = NotificationCenter.default.addObserver(
                    forName: NSApplication.willTerminateNotification,
                    object: nil,
                    queue: .main
                ) { [weak self] _ in
                    self?.stopSlackBridge()
                }
            }
        } catch {
            slackBridgeStatus = "Could not start bridge: \(error.localizedDescription)"
        }
    }

    func stopSlackBridge() {
        guard let process = slackBridgeProcess, process.isRunning else {
            slackBridgeStatus = "Stopped"
            return
        }
        process.terminate()
        slackBridgeStatus = "Stopping…"
    }

    private var appShortcuts: [GlobalHotKey] = []

    override init() {
        super.init()
        installAppShortcut()  // ready as soon as Helm launches
        DispatchQueue.main.async { AgentCursor.shared.start() }
        DispatchQueue.main.async { [weak self] in self?.autoStartConfiguredBridges() }
        UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound]) { _, _ in }
    }

    /// A bridge can now be left running unattended (see "start automatically"), so an
    /// unexpected exit — not a manual Stop, which exits 0 — needs to reach the user even if
    /// Settings isn't open. The bridge's own log already has the detail; this just flags it.
    func notifyBridgeStopped(platform: String, code: Int32) {
        let content = UNMutableNotificationContent()
        content.title = "Helm: \(platform) bridge stopped"
        content.body = "It exited unexpectedly (code \(code)). Open Settings to check its log and restart it."
        content.sound = .default
        let request = UNNotificationRequest(identifier: "helm.bridge-stopped.\(platform)", content: content, trigger: nil)
        UNUserNotificationCenter.current().add(request)
    }

    /// Starts any bridge whose "start automatically" toggle is on. Each start method already
    /// no-ops safely (just sets a status string) if its token/IDs aren't actually saved yet, so
    /// a stale toggle from before a bridge was configured can't do anything harmful.
    private func autoStartConfiguredBridges() {
        let defaults = UserDefaults.standard
        if defaults.bool(forKey: "whatsapp.auto-start") { startWhatsAppBridge() }
        if defaults.bool(forKey: "telegram.auto-start") { startTelegramBridge() }
        if defaults.bool(forKey: "discord.auto-start") { startDiscordBridge() }
        if defaults.bool(forKey: "slack.auto-start") { startSlackBridge() }
    }

    /// helm://ask?q=... runs a command as if it were typed into Helm. Off unless the user enables
    /// developerURLCommands, because any web page could otherwise send Helm commands.
    func handleOpenURL(_ url: URL) {
        guard UserDefaults.standard.bool(forKey: "developerURLCommands"),
              let components = URLComponents(url: url, resolvingAgainstBaseURL: false), components.host == "ask",
              let query = components.queryItems?.first(where: { $0.name == "q" })?.value else { return }
        draft = query
        send()
    }

    /// Control+Shift+§ opens Helm from anywhere (§ is the ISO key left of 1; ` on US keyboards).
    func installAppShortcut() {
        guard appShortcuts.isEmpty else { return }
        let modifiers = UInt32(controlKey | shiftKey)
        appShortcuts = [UInt32(kVK_ISO_Section), UInt32(kVK_ANSI_Grave)].enumerated().map { index, key in
            GlobalHotKey(keyCode: key, modifiers: modifiers, id: UInt32(index + 1)) { [weak self] in self?.showHUD() }
        }
        // Both § and ` are registered so either keyboard layout works; only warn if neither bound
        // (e.g. another app already claimed both), since a single miss is expected and harmless.
        if appShortcuts.allSatisfy({ $0.registrationError != nil }) {
            lines.append(ChatLine(
                role: .error,
                text: "Could not register Control+Shift+§ to open Helm (another app may be using it). Open Helm from the menu bar instead."
            ))
        }
        // Control+Shift+D: start/stop dictation into Helm's command box.
        let dictationShortcut = GlobalHotKey(keyCode: UInt32(kVK_ANSI_D), modifiers: modifiers, id: 3) { [weak self] in
            self?.showHUD()
            self?.toggleDictation()
        }
        appShortcuts.append(dictationShortcut)
        if let error = dictationShortcut.registrationError {
            lines.append(ChatLine(
                role: .error,
                text: "Could not register Control+Shift+D for dictation (Carbon status \(error)). Use the mic button in the HUD instead."
            ))
        }
        let stopShortcut = GlobalHotKey(keyCode: UInt32(kVK_Escape), modifiers: modifiers, id: 4) { [weak self] in
            self?.emergencyStop()
        }
        appShortcuts.append(stopShortcut)
        emergencyStopShortcutAvailable = stopShortcut.registrationError == nil
        if let error = stopShortcut.registrationError {
            lines.append(ChatLine(
                role: .error,
                text: "Could not register the emergency-stop shortcut (Carbon status \(error)). Use the stop button while a task runs."
            ))
        }
    }

    func showHUD() {
        if #available(macOS 14.0, *) { NSApp.activate() } else { NSApp.activate(ignoringOtherApps: true) }
        if let window = NSApp.windows.first(where: { $0.title == "Helm" && !($0 is NSPanel) }) {
            // macOS may refuse to switch apps for a background hotkey; show the window on top anyway.
            window.orderFrontRegardless()
            window.makeKeyAndOrderFront(nil)
        } else {
            hudRequests += 1  // window was closed: ask SwiftUI to reopen it
        }
    }
    /// "hud" = full window, "menubar" = compact popup from the menu bar icon.
    @Published var interfaceStyle = UserDefaults.standard.string(forKey: "interfaceStyle") ?? "hud" {
        didSet { UserDefaults.standard.set(interfaceStyle, forKey: "interfaceStyle") }
    }

    /// Screen Recording grants only take effect after a relaunch.
    static func relaunch() {
        let configuration = NSWorkspace.OpenConfiguration()
        configuration.createsNewApplicationInstance = true
        NSWorkspace.shared.openApplication(at: Bundle.main.bundleURL, configuration: configuration) { _, _ in
            DispatchQueue.main.async { NSApp.terminate(nil) }
        }
    }
    @Published var savedKeys = Set(ProviderOption.all.compactMap { KeyStore.exists($0.id) ? $0.id : nil })

    /// Model per provider; empty means the provider's default.
    func model(for provider: String) -> String {
        UserDefaults.standard.string(forKey: "model.\(provider)") ?? ""
    }

    func setModel(_ model: String, for provider: String) {
        UserDefaults.standard.set(model.trimmingCharacters(in: .whitespaces), forKey: "model.\(provider)")
        objectWillChange.send()
    }

    func saveKey(_ key: String, for provider: String) -> Bool {
        let trimmed = key.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty, KeyStore.save(trimmed, for: provider) else { return false }
        savedKeys.insert(provider)
        return true
    }

    func removeKey(for provider: String) {
        KeyStore.delete(provider)
        savedKeys.remove(provider)
    }

    var brainLabel: String {
        let option = ProviderOption.find(provider)
        let chosen = model(for: provider)
        return "\(option.label) · \(chosen.isEmpty ? option.defaultModel : chosen)"
    }

    private var process: Process?
    private var inputPipe: Pipe?
    private var buffer = ""
    private let voice = VoiceInputController()
    private let speech = AVSpeechSynthesizer()
    private var voiceConfigured = false

    var projectRoot: URL {
        if let root = ProcessInfo.processInfo.environment["ASSISTANT_PROJECT_DIR"] {
            return URL(fileURLWithPath: root, isDirectory: true)
        }
        return URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
    }

    private var historyURL: URL {
        FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Application Support/Local Desktop Assistant/chat_history.json")
    }

    func configureVoice() {
        guard onboarded, !voiceConfigured else { return }  // no permission prompts before setup
        voiceConfigured = true
        voice.onStatusChange = { [weak self] listening, status in
            guard let self else { return }
            if listening, self.speech.isSpeaking {
                self.speech.stopSpeaking(at: .immediate)  // barge-in: talking over Helm interrupts it
            }
            self.isListening = listening
            self.voiceStatus = listening ? "Listening…" : status
        }
        voice.onError = { [weak self] message in
            self?.lines.append(ChatLine(role: .error, text: message))
        }
        voice.onPartialTranscript = { [weak self] transcript in
            guard let self else { return }
            self.draft = self.dictating
                ? [self.dictationBase, transcript].filter { !$0.isEmpty }.joined(separator: " ")
                : transcript
        }
        voice.onTranscript = { [weak self] transcript, _ in
            guard let self else { return }
            if self.dictating {
                // Dictation fills the command box so you can edit before sending.
                self.draft = [self.dictationBase, transcript].filter { !$0.isEmpty }.joined(separator: " ")
                self.dictating = false
            } else {
                self.draft = transcript
                self.send()
            }
        }
        if voiceHotkey { voice.installHotkey() }
    }

    func toggleVoice() {
        configureVoice()
        voice.toggleCapture()
    }

    /// Talk, and the words appear in the command box (not sent) — Control+Shift+D or the mic button.
    func toggleDictation() {
        configureVoice()
        if !isListening {
            dictating = true
            dictationBase = draft.trimmingCharacters(in: .whitespacesAndNewlines)
        }
        voice.toggleCapture()
    }

    func send() {
        let text = draft.trimmingCharacters(in: .whitespacesAndNewlines)
        if text.lowercased().hasPrefix("agent:") {  // "agent: research X" runs in the background
            draft = String(text.dropFirst("agent:".count))
            dispatchDraft()
            return
        }
        guard !text.isEmpty, process == nil else { return }
        draft = ""
        lines.append(ChatLine(role: .user, text: text))
        logBackend("\n=== \(Date()) user submitted a request\n")
        var arguments = ["--chat", text, "--autonomy", askBeforeActing ? "approval" : "autonomous", "--provider", provider]
        if !model(for: provider).isEmpty { arguments += ["--model", model(for: provider)] }
        if !disabledTools.isEmpty { arguments += ["--disable-tools", disabledTools.sorted().joined(separator: ",")] }
        if cloudVision { arguments.append("--cloud-vision") }
        arguments += ["--browser", browser]
        launch(arguments)
    }

    func newChat() {
        stop()
        lines = []
        try? FileManager.default.removeItem(at: historyURL)
    }

    func approve(all: Bool = false) { respond(all ? "all\n" : "y\n") }

    func reject() { respond("n\n") }

    func stop() {
        pendingApproval = nil
        process?.terminate()
    }

    var hasActiveTasks: Bool {
        process?.isRunning == true || agents.contains(where: \.isRunning)
    }

    func emergencyStop() {
        let hadActiveTasks = hasActiveTasks
        stop()
        agents.filter(\.isRunning).forEach { $0.stop() }
        speech.stopSpeaking(at: .immediate)
        cancelActiveBridgeTasks()
        if hadActiveTasks {
            addActivity("Emergency stop: stopped all running tasks. (⌃⇧Esc)")
        }
    }

    /// Cancels any in-flight remote task a bridge is running, without stopping the bridge
    /// itself — a task triggered from WhatsApp/Telegram/Discord/Slack while you were away is
    /// covered by the same local kill switch, not just tasks started from this Mac.
    private func cancelActiveBridgeTasks() {
        for process in [whatsappBridgeProcess, telegramBridgeProcess, discordBridgeProcess, slackBridgeProcess] {
            guard let process, process.isRunning else { continue }
            kill(process.processIdentifier, SIGUSR1)
        }
    }

    private func respond(_ answer: String) {
        guard let inputPipe, pendingApproval != nil else { return }
        inputPipe.fileHandleForWriting.write(Data(answer.utf8))
        pendingApproval = nil
        if answer != "n\n" { stepAside() }  // clicking Allow made Helm active again
    }

    /// Tools that must bring another app to the front.
    private static let actingTools: Set<String> = [
        "open_app", "open_url", "send_message", "read_chat", "control_screen", "resume_task", "click_in_app", "menu_in_app",
        "type_in_app", "keys_in_app", "new_document", "paste_into_app", "web_search", "ask_chatgpt", "media_control",
    ]

    /// macOS won't let a background process pull focus away from the app the user is typing in,
    /// and that app is Helm itself. Give up focus (the window stays visible) so the target can come forward.
    private func stepAside() {
        NSApp.deactivate()
    }

    private func logBackend(_ text: String) {
        let folder = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/Logs/Helm", isDirectory: true)
        let url = folder.appendingPathComponent("backend.log")
        let redacted = Self.redactLogSecrets(text)
        do {
            try FileManager.default.createDirectory(
                at: folder,
                withIntermediateDirectories: true,
                attributes: [.posixPermissions: 0o700]
            )
            if !FileManager.default.fileExists(atPath: url.path) {
                try Data().write(to: url, options: .atomic)
            }
            try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: url.path)
            let handle = try FileHandle(forWritingTo: url)
            handle.seekToEndOfFile()
            handle.write(Data(redacted.utf8))
            try handle.close()
        } catch {
            lines.append(ChatLine(role: .error, text: "Could not securely write the backend log: \(error.localizedDescription)"))
        }
    }

    private static func redactLogSecrets(_ text: String) -> String {
        let patterns: [(String, String)] = [
            (#"(?i)(\b(?:api[_ -]?key|access[_ -]?token|refresh[_ -]?token|password|passphrase|secret|authorization)\b(?:\s+(?:is\s+)?|[:=]\s*))[^\s,;]+"#, "$1[REDACTED]"),
            (#"(?i)(\bBearer\s+)[A-Za-z0-9._~+/=-]+"#, "$1[REDACTED]"),
            (#"\bsk-(?:ant-)?[A-Za-z0-9_-]{16,}\b"#, "[REDACTED]"),
            (#"\bgh[pousr]_[A-Za-z0-9_]{20,}\b"#, "[REDACTED]"),
            (#"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"#, "[REDACTED]"),
            (#"\bAIza[A-Za-z0-9_-]{30,}\b"#, "[REDACTED]"),
            (#"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"#, "[REDACTED]"),
        ]
        return patterns.reduce(text) { value, item in
            value.replacingOccurrences(
                of: item.0,
                with: item.1,
                options: .regularExpression
            )
        }
    }

    private func launch(_ arguments: [String]) {
        let python = projectRoot.appendingPathComponent(".venv/bin/python")
        let script = projectRoot.appendingPathComponent("assistant.py")
        guard FileManager.default.isExecutableFile(atPath: python.path) else {
            lines.append(ChatLine(role: .error, text: "Project Python not found. Set up .venv using the README."))
            return
        }
        let child = Process()
        let output = Pipe()
        let input = Pipe()
        child.executableURL = python
        child.arguments = ["-u", script.path] + arguments
        child.currentDirectoryURL = projectRoot
        child.standardOutput = output
        child.standardError = output
        child.standardInput = input
        var environment = ProcessInfo.processInfo.environment
        if provider != "ollama", let key = KeyStore.read(provider) {
            environment["HELM_API_KEY"] = key
        }
        if UserDefaults.standard.bool(forKey: "whatsapp.notify-local-tasks"),
           let accessToken = KeyStore.readSecret("whatsapp.access-token"),
           let phoneNumberID = UserDefaults.standard.string(forKey: "whatsapp.phone-number-id"),
           let ownerNumber = UserDefaults.standard.string(forKey: "whatsapp.owner-number") {
            environment["HELM_WHATSAPP_NOTIFY"] = "1"
            environment["HELM_WHATSAPP_ACCESS_TOKEN"] = accessToken
            environment["HELM_WHATSAPP_PHONE_NUMBER_ID"] = phoneNumberID
            environment["HELM_WHATSAPP_OWNER_NUMBER"] = ownerNumber
            environment["HELM_WHATSAPP_API_VERSION"] =
                UserDefaults.standard.string(forKey: "whatsapp.api-version") ?? "v23.0"
        }
        child.environment = environment
        child.terminationHandler = { [weak self] finished in
            DispatchQueue.main.async { self?.finish(status: finished.terminationStatus) }
        }
        output.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            if data.isEmpty {
                handle.readabilityHandler = nil
                return
            }
            let text = String(decoding: data, as: UTF8.self)
            DispatchQueue.main.async { self?.receive(text) }
        }
        do {
            try child.run()
            process = child
            inputPipe = input
            buffer = ""
            isThinking = true
        } catch {
            lines.append(ChatLine(role: .error, text: "Could not start Helm: \(error.localizedDescription)"))
        }
    }

    private func receive(_ text: String) {
        logBackend(text)
        buffer += text
        let parts = buffer.components(separatedBy: .newlines)
        buffer = parts.last ?? ""
        for line in parts.dropLast() where !line.trimmingCharacters(in: .whitespaces).isEmpty {
            if line.hasPrefix("APPROVAL_REQUIRED: ") {
                pendingApproval = String(line.dropFirst("APPROVAL_REQUIRED: ".count))
            } else if line.hasPrefix("CHAT_REPLY_JSON:") {
                let json = Data(line.dropFirst("CHAT_REPLY_JSON:".count).utf8)
                if let reply = try? JSONDecoder().decode(String.self, from: json) {
                    lines.append(ChatLine(role: .bot, text: reply))
                    AgentCursor.shared.say(reply)
                    speak(reply)
                }
            } else if line.hasPrefix("CURSOR: ") {
                // {"x":402,"y":722,"action":"click 7","click":true}; x/y omitted = stay put
                if let data = line.dropFirst("CURSOR: ".count).data(using: .utf8),
                   let info = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
                    let point = (info["x"] as? Double).flatMap { x in (info["y"] as? Double).map { CGPoint(x: x, y: $0) } }
                    AgentCursor.shared.move("Helm", to: point, action: info["action"] as? String ?? "",
                                            click: info["click"] as? Bool ?? false)
                }
            } else if line.hasPrefix("TOOL: ") {
                let tool = line.dropFirst("TOOL: ".count).split(separator: " ").first.map(String.init) ?? ""
                if Self.actingTools.contains(tool) { stepAside() }
                let name = line.dropFirst("TOOL: ".count).split(separator: " ").first.map(String.init) ?? ""
                addActivity(Self.toolLabels[name] ?? "Working on it…")
            } else if line.hasPrefix("Error:") || line.hasPrefix("SCREEN_ACCESS_DENIED:") {
                lines.append(ChatLine(role: .error, text: line))
            } else if line.hasPrefix("WHATSAPP_NOTIFY_ERROR:") {
                lines.append(ChatLine(role: .error, text: String(line.dropFirst("WHATSAPP_NOTIFY_ERROR:".count))))
            } else if !line.hasPrefix("Allow this action?") {
                addActivity(line)
            }
        }
    }

    private static let toolLabels = [
        "open_app": "Opening app",
        "open_url": "Opening website",
        "set_volume": "Adjusting volume",
        "system_status": "Checking system status",
        "control_screen": "Taking control of the screen",
        "resume_task": "Resuming the paused task",
        "web_search": "Searching in your browser",
        "media_control": "Controlling music",
    ]

    private func addActivity(_ text: String) {
        if let last = lines.last, last.role == .activity {
            lines[lines.count - 1].text += "\n" + text
        } else {
            lines.append(ChatLine(role: .activity, text: text))
        }
    }

    private func finish(status: Int32) {
        if !buffer.isEmpty { receive("\n") }
        if status != 0, lines.last?.role != .error {
            lines.append(ChatLine(role: .error, text: "Stopped (exit code \(status))."))
        }
        process = nil
        inputPipe = nil
        pendingApproval = nil
        isThinking = false
    }

    func speak(_ text: String) {
        guard speakReplies else { return }
        let utterance = AVSpeechUtterance(string: text)
        utterance.voice = AVSpeechSynthesisVoice(identifier: "com.apple.voice.compact.en-GB.Daniel")
            ?? AVSpeechSynthesisVoice(language: "en-GB")
        speech.stopSpeaking(at: .immediate)
        speech.speak(utterance)
    }
}

/// The menu bar icon. It is always alive, so it is where the global shortcut reopens the HUD.
struct MenuBarIcon: View {
    @EnvironmentObject private var chat: ChatController
    @Environment(\.openWindow) private var openWindow

    var body: some View {
        Image(systemName: chat.isListening ? "waveform" : "circle.hexagongrid.fill")
            .onChange(of: chat.hudRequests) { _ in
                openWindow(id: "hud")
                NSApp.activate(ignoringOtherApps: true)
            }
            .onOpenURL { chat.handleOpenURL($0) }
    }
}

/// In HUD mode the menu bar icon is just a launcher, so there is only one chat UI.
struct MenuLauncher: View {
    @EnvironmentObject private var chat: ChatController
    @Environment(\.openWindow) private var openWindow

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(spacing: 10) {
                Orb(active: chat.isThinking || chat.isListening, size: 26)
                VStack(alignment: .leading, spacing: 1) {
                    Text("HELM").font(.system(size: 12, weight: .bold, design: .monospaced)).tracking(3)
                    Text(chat.isThinking ? "Working…" : chat.onboarded ? "Online" : "Setup not finished")
                        .font(.system(size: 10)).foregroundStyle(.secondary)
                }
            }
            Button {
                openWindow(id: "hud")
                NSApp.activate(ignoringOtherApps: true)
            } label: {
                Label(chat.onboarded ? "Open Helm" : "Continue setup", systemImage: "macwindow")
                    .frame(maxWidth: .infinity)
            }
            .buttonStyle(.borderedProminent)
            .tint(jarvisCyan)
            HStack {
                Button("Settings") {
                    chat.showProviderSettings = true
                    openWindow(id: "hud")
                    NSApp.activate(ignoringOtherApps: true)
                }
                Spacer()
                Button("Quit") { NSApplication.shared.terminate(nil) }
            }
            .controlSize(.small)
        }
        .padding(16)
        .preferredColorScheme(.dark)
    }
}

/// Chat by default; the original panel stays reachable for permissions and MCP.
struct ChatRootView: View {
    @EnvironmentObject private var chat: ChatController
    @State private var showAdvanced = false

    var body: some View {
        if chat.interfaceStyle == "hud" || !chat.onboarded {
            MenuLauncher().frame(width: 280)
        } else {
            panel.frame(width: 420, height: 640)
        }
    }

    @ViewBuilder
    private var panel: some View {
        if showAdvanced {
            VStack(spacing: 0) {
                HStack {
                    Button {
                        showAdvanced = false
                    } label: {
                        Label("Back to chat", systemImage: "chevron.left")
                    }
                    .buttonStyle(.plain)
                    .foregroundStyle(jarvisCyan)
                    Spacer()
                }
                .padding(.horizontal, 20)
                .padding(.top, 14)
                AssistantPanel()
            }
        } else {
            ChatPanel(showAdvanced: $showAdvanced)
        }
    }
}

struct ChatPanel: View {
    @EnvironmentObject private var chat: ChatController
    @Environment(\.openWindow) private var openWindow
    @Binding var showAdvanced: Bool

    private let suggestions = [
        "What's my battery and the time?",
        "Open Spotify",
        "Set the volume to 30",
        "Search YouTube for lo-fi music",
    ]

    var body: some View {
        VStack(spacing: 0) {
            header
            Rectangle().fill(jarvisCyan.opacity(0.12)).frame(height: 1)
            transcript
            if let action = chat.pendingApproval {
                approvalCard(action)
            }
            inputBar
        }
        .background(
            LinearGradient(
                colors: [Color(red: 0.03, green: 0.05, blue: 0.10), Color(red: 0.05, green: 0.09, blue: 0.17)],
                startPoint: .top,
                endPoint: .bottom
            )
        )
        .preferredColorScheme(.dark)
        .onAppear { chat.configureVoice() }
    }

    private var status: String {
        if chat.isListening { return "Listening…" }
        if chat.pendingApproval != nil { return "Waiting for your OK" }
        if chat.isThinking { return "Thinking…" }
        return "Online · running on this Mac"
    }

    private var header: some View {
        HStack(spacing: 12) {
            Orb(active: chat.isThinking || chat.isListening, size: 30)
            VStack(alignment: .leading, spacing: 2) {
                Text("HELM")
                    .font(.system(size: 13, weight: .bold, design: .rounded))
                    .tracking(3)
                Text(status)
                    .font(.system(size: 10))
                    .foregroundStyle(.secondary)
            }
            Spacer()
            Menu {
                Toggle("Ask before screen actions", isOn: $chat.askBeforeActing)
                Toggle("Speak replies", isOn: $chat.speakReplies)
                if chat.hasActiveTasks {
                    Button(
                        "Emergency stop all tasks\(chat.emergencyStopShortcutAvailable ? "  ⌃⇧Esc" : "")",
                        role: .destructive
                    ) {
                        chat.emergencyStop()
                    }
                }
                Divider()
                Button("New chat") { chat.newChat() }
                Button("AI model & API keys…") {
                    chat.showProviderSettings = true
                    openWindow(id: "hud")
                    NSApp.activate(ignoringOtherApps: true)
                }
                Button("Open full HUD") {
                    openWindow(id: "hud")
                    NSApp.activate(ignoringOtherApps: true)
                }
                Button("Permissions & MCP tools…") { showAdvanced = true }
                Divider()
                Button("Quit Helm") { NSApplication.shared.terminate(nil) }
            } label: {
                Image(systemName: "slider.horizontal.3")
                    .font(.system(size: 13))
                    .foregroundStyle(.secondary)
            }
            .menuStyle(.borderlessButton)
            .menuIndicator(.hidden)
            .fixedSize()
            .accessibilityLabel("Settings")
        }
        .padding(.horizontal, 18)
        .padding(.vertical, 14)
    }

    private var transcript: some View {
        ScrollViewReader { proxy in
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 10) {
                    if chat.lines.isEmpty {
                        welcome
                    }
                    ForEach(chat.lines) { line in
                        bubble(line).id(line.id)
                    }
                    if chat.isThinking && chat.pendingApproval == nil {
                        TypingDots().id("typing")
                    }
                }
                .padding(16)
            }
            .onChange(of: chat.lines) { _ in
                withAnimation(.easeOut(duration: 0.2)) {
                    proxy.scrollTo(chat.isThinking ? AnyHashable("typing") : AnyHashable(chat.lines.last?.id), anchor: .bottom)
                }
            }
        }
    }

    private var welcome: some View {
        VStack(spacing: 14) {
            Orb(active: false, size: 74).padding(.top, 36)
            Text("How can I help?")
                .font(.system(size: 18, weight: .semibold, design: .rounded))
            Text("Ask anything, or tell me what to do on your Mac.")
                .font(.system(size: 12))
                .foregroundStyle(.secondary)
            VStack(spacing: 8) {
                ForEach(suggestions, id: \.self) { suggestion in
                    Button {
                        chat.draft = suggestion
                        chat.send()
                    } label: {
                        Text(suggestion)
                            .font(.system(size: 12))
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .padding(.horizontal, 12)
                            .padding(.vertical, 9)
                            .background(Color.white.opacity(0.05), in: RoundedRectangle(cornerRadius: 10))
                            .overlay(RoundedRectangle(cornerRadius: 10).strokeBorder(jarvisCyan.opacity(0.18)))
                    }
                    .buttonStyle(.plain)
                }
            }
            .padding(.top, 8)
        }
        .frame(maxWidth: .infinity)
    }

    @ViewBuilder
    private func bubble(_ line: ChatLine) -> some View {
        switch line.role {
        case .user:
            HStack {
                Spacer(minLength: 50)
                Text(line.text)
                    .font(.system(size: 13))
                    .textSelection(.enabled)
                    .padding(.horizontal, 13)
                    .padding(.vertical, 9)
                    .background(
                        LinearGradient(colors: [jarvisBlue, jarvisCyan.opacity(0.8)], startPoint: .topLeading, endPoint: .bottomTrailing),
                        in: RoundedRectangle(cornerRadius: 15)
                    )
            }
        case .bot:
            HStack {
                Text(line.text)
                    .font(.system(size: 13))
                    .textSelection(.enabled)
                    .padding(.horizontal, 13)
                    .padding(.vertical, 9)
                    .background(Color.white.opacity(0.07), in: RoundedRectangle(cornerRadius: 15))
                    .overlay(RoundedRectangle(cornerRadius: 15).strokeBorder(jarvisCyan.opacity(0.15)))
                Spacer(minLength: 50)
            }
        case .activity:
            Label {
                Text(line.text).textSelection(.enabled)
            } icon: {
                Image(systemName: "gearshape.2").foregroundStyle(jarvisCyan)
            }
            .font(.system(size: 11, design: .monospaced))
            .foregroundStyle(.secondary)
            .padding(.leading, 4)
        case .error:
            Label(line.text, systemImage: "exclamationmark.triangle.fill")
                .font(.system(size: 11))
                .foregroundStyle(.orange)
                .textSelection(.enabled)
        }
    }

    private func approvalCard(_ action: String) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            Label("May I \(action)?", systemImage: "hand.raised.fill")
                .font(.system(size: 12, weight: .semibold))
                .foregroundStyle(.orange)
            HStack {
                Button("Stop") { chat.reject() }
                Button("Allow all") { chat.approve(all: true) }
                    .help("Allow every remaining step of this task.")
                Spacer()
                Button("Allow") { chat.approve() }
                    .buttonStyle(.borderedProminent)
                    .keyboardShortcut(.return, modifiers: [.command])
            }
            .controlSize(.small)
        }
        .padding(12)
        .background(Color.orange.opacity(0.10), in: RoundedRectangle(cornerRadius: 12))
        .padding(.horizontal, 14)
        .padding(.bottom, 8)
    }

    private var inputBar: some View {
        HStack(spacing: 10) {
            Button {
                chat.toggleVoice()
            } label: {
                Image(systemName: chat.isListening ? "waveform" : "mic.fill")
                    .font(.system(size: 13, weight: .semibold))
                    .foregroundStyle(chat.isListening ? .white : jarvisCyan)
                    .frame(width: 32, height: 32)
                    .background(chat.isListening ? Color.red : jarvisCyan.opacity(0.12), in: Circle())
            }
            .buttonStyle(.plain)
            .help("Talk (or hold Control+Option)")
            .accessibilityLabel(chat.isListening ? "Stop listening" : "Start voice input")

            TextField("Message Helm…", text: $chat.draft, axis: .vertical)
                .textFieldStyle(.plain)
                .font(.system(size: 13))
                .lineLimit(1...4)
                .onSubmit { chat.send() }
                .disabled(chat.isThinking)

            if chat.isThinking {
                Button { chat.stop() } label: {
                    Image(systemName: "stop.circle.fill").font(.system(size: 24))
                }
                .buttonStyle(.plain)
                .foregroundStyle(.red.opacity(0.85))
                .help("Stop")
            } else {
                Button { chat.send() } label: {
                    Image(systemName: "arrow.up.circle.fill").font(.system(size: 24))
                }
                .buttonStyle(.plain)
                .foregroundStyle(jarvisCyan)
                .disabled(chat.draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                .help("Send")
            }
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 9)
        .background(Color.white.opacity(0.06), in: RoundedRectangle(cornerRadius: 18))
        .overlay(RoundedRectangle(cornerRadius: 18).strokeBorder(jarvisCyan.opacity(0.22)))
        .padding(14)
    }
}

private struct Orb: View {
    let active: Bool
    let size: CGFloat
    @State private var pulse = false

    var body: some View {
        ZStack {
            Circle()
                .fill(jarvisCyan.opacity(active ? 0.45 : 0.25))
                .blur(radius: size * 0.3)
            Circle()
                .fill(AngularGradient(colors: [jarvisCyan, jarvisBlue, jarvisCyan], center: .center))
            Circle()
                .strokeBorder(Color.white.opacity(0.6), lineWidth: max(1, size * 0.04))
                .padding(size * 0.18)
        }
        .frame(width: size, height: size)
        .scaleEffect(pulse ? 1.06 : 0.94)
        .animation(.easeInOut(duration: active ? 0.5 : 1.6).repeatForever(autoreverses: true), value: pulse)
        .onAppear { pulse = true }
        .accessibilityHidden(true)
    }
}

private struct TypingDots: View {
    @State private var phase = false

    var body: some View {
        HStack(spacing: 5) {
            ForEach(0..<3) { index in
                Circle()
                    .fill(jarvisCyan)
                    .frame(width: 6, height: 6)
                    .opacity(phase ? 1 : 0.25)
                    .animation(.easeInOut(duration: 0.6).repeatForever().delay(Double(index) * 0.2), value: phase)
            }
        }
        .padding(.horizontal, 13)
        .padding(.vertical, 11)
        .background(Color.white.opacity(0.07), in: RoundedRectangle(cornerRadius: 15))
        .onAppear { phase = true }
        .accessibilityLabel("Helm is thinking")
    }
}

// MARK: - Full-screen HUD

/// Big Jarvis-style window: reactor and status on the left, conversation on the right.
struct HUDView: View {
    @EnvironmentObject private var chat: ChatController
    @State private var showAdvanced = false

    var body: some View {
        ZStack {
            Color(red: 0.01, green: 0.02, blue: 0.05).ignoresSafeArea()
            RadialGradient(
                colors: [jarvisCyan.opacity(0.10), .clear],
                center: .init(x: 0.25, y: 0.45),
                startRadius: 10,
                endRadius: 520
            )
            .ignoresSafeArea()
            GridBackdrop().opacity(0.35).ignoresSafeArea()
            if !chat.onboarded {
                OnboardingView()
            } else if showAdvanced {
                ChatRootViewAdvanced(showAdvanced: $showAdvanced)
            } else {
                HStack(spacing: 0) {
                    reactorColumn
                        .frame(width: 340)
                    Rectangle().fill(jarvisCyan.opacity(0.15)).frame(width: 1)
                    conversationColumn
                }
            }
        }
        .preferredColorScheme(.dark)
        .frame(minWidth: 820, minHeight: 560)
        .sheet(isPresented: $chat.showProviderSettings) {
            SettingsView().preferredColorScheme(.dark)
        }
        .onOpenURL { chat.handleOpenURL($0) }
        .onAppear {
            chat.configureVoice()
            // Menu bar style hides the big window at launch, but not when the shortcut asked for it.
            if chat.onboarded && chat.interfaceStyle == "menubar" && chat.hudRequests == 0 { closeHUDWindow() }
        }
        .onChange(of: chat.interfaceStyle) { style in
            if style == "menubar" && chat.onboarded && !chat.showProviderSettings { closeHUDWindow() }
        }
    }

    private func closeHUDWindow() {
        DispatchQueue.main.async {
            NSApp.windows.filter { $0.title == "Helm" && !($0 is NSPanel) }.forEach { $0.close() }
        }
    }

    private var stateText: String {
        if chat.isListening { return "LISTENING" }
        if chat.pendingApproval != nil { return "AWAITING AUTHORISATION" }
        if chat.isThinking { return "PROCESSING" }
        return "STANDING BY"
    }

    private var reactorColumn: some View {
        VStack(spacing: 22) {
            HStack {
                Text("H · E · L · M")
                    .font(.system(size: 12, weight: .bold, design: .monospaced))
                    .foregroundStyle(jarvisCyan)
                Spacer()
            }
            Spacer()
            Button { chat.toggleVoice() } label: {
                Reactor(active: chat.isThinking || chat.isListening, listening: chat.isListening)
                    .frame(width: 230, height: 230)
            }
            .buttonStyle(.plain)
            .help("Click to talk, or hold Control+Option")
            .accessibilityLabel(chat.isListening ? "Stop listening" : "Start voice input")
            Text(stateText)
                .font(.system(size: 13, weight: .semibold, design: .monospaced))
                .tracking(4)
                .foregroundStyle(chat.isListening ? .red : jarvisCyan)
            TimelineView(.periodic(from: .now, by: 1)) { context in
                VStack(spacing: 4) {
                    Text(context.date, format: .dateTime.hour().minute().second())
                        .font(.system(size: 28, weight: .light, design: .monospaced))
                    Text(context.date, format: .dateTime.weekday(.wide).day().month(.wide))
                        .font(.system(size: 11, design: .monospaced))
                        .foregroundStyle(.secondary)
                }
            }
            Spacer()
            VStack(alignment: .leading, spacing: 6) {
                telemetry("BRAIN", chat.brainLabel)
                telemetry("WEB ACCESS", chat.isEnabled(["web_search"]) ? "ON" : "OFF")
                telemetry("SCREEN CONTROL", chat.askBeforeActing ? "ASK FIRST" : "FULL AUTONOMY")
                telemetry("VOICE", chat.speakReplies ? "ON" : "MUTED")
            }
        }
        .padding(26)
    }

    private func telemetry(_ label: String, _ value: String) -> some View {
        HStack {
            Text(label).foregroundStyle(.secondary)
            Spacer()
            Text(value).foregroundStyle(jarvisCyan)
        }
        .font(.system(size: 10, design: .monospaced))
    }

    private var conversationColumn: some View {
        VStack(spacing: 0) {
            HStack {
                Text("COMMS LOG")
                    .font(.system(size: 11, weight: .semibold, design: .monospaced))
                    .tracking(3)
                    .foregroundStyle(.secondary)
                Spacer()
                Toggle("Full autonomy", isOn: Binding(
                    get: { !chat.askBeforeActing },
                    set: { chat.askBeforeActing = !$0 }
                ))
                .toggleStyle(.switch)
                .controlSize(.mini)
                .font(.system(size: 11, design: .monospaced))
                .help("When on, Helm clicks and types without asking first.")
                Menu {
                    Button("Settings (brain, abilities, permissions)…") { chat.showProviderSettings = true }
                    Toggle("Speak replies", isOn: $chat.speakReplies)
                    if chat.hasActiveTasks {
                        Button(
                            "Emergency stop all tasks\(chat.emergencyStopShortcutAvailable ? "  ⌃⇧Esc" : "")",
                            role: .destructive
                        ) {
                            chat.emergencyStop()
                        }
                    }
                    Button("New chat") { chat.newChat() }
                    Button("Permissions & MCP tools…") { showAdvanced = true }
                    Divider()
                    Button("Quit Helm") { NSApplication.shared.terminate(nil) }
                } label: {
                    Image(systemName: "slider.horizontal.3").foregroundStyle(.secondary)
                }
                .menuStyle(.borderlessButton)
                .menuIndicator(.hidden)
                .fixedSize()
                .padding(.leading, 12)
                .accessibilityLabel("Settings")
            }
            .padding(.horizontal, 24)
            .padding(.vertical, 16)
            ScrollViewReader { proxy in
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 16) {
                        if chat.lines.isEmpty { hudWelcome }
                        ForEach(chat.lines) { line in
                            hudLine(line).id(line.id)
                        }
                        if chat.isThinking && chat.pendingApproval == nil {
                            TypingDots().id("typing")
                        }
                    }
                    .padding(.horizontal, 24)
                    .padding(.vertical, 8)
                }
                .onChange(of: chat.lines) { _ in
                    withAnimation(.easeOut(duration: 0.2)) {
                        proxy.scrollTo(chat.isThinking ? AnyHashable("typing") : AnyHashable(chat.lines.last?.id), anchor: .bottom)
                    }
                }
            }
            if let action = chat.pendingApproval {
                HUDApproval(action: action)
            }
            AgentsBoard().padding(.horizontal, 24)
            HUDInput()
        }
    }

    private var greeting: String {
        switch Calendar.current.component(.hour, from: Date()) {
        case 5..<12: return "Good morning."
        case 12..<18: return "Good afternoon."
        default: return "Good evening."
        }
    }

    private var hudWelcome: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text(greeting)
                .font(.system(size: 26, weight: .light))
            Text("Say or type a command. I can open apps and websites, search the web, YouTube and TikTok, control your music and volume, and drive the screen with the mouse and keyboard.")
                .font(.system(size: 13))
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            LazyVGrid(columns: [GridItem(.flexible()), GridItem(.flexible())], spacing: 10) {
                ForEach(["What's my battery and the time?", "Find TikTok videos about latte art", "Play some music", "Search YouTube for SwiftUI tutorials"], id: \.self) { suggestion in
                    Button {
                        chat.draft = suggestion
                        chat.send()
                    } label: {
                        Text(suggestion)
                            .font(.system(size: 12, design: .monospaced))
                            .frame(maxWidth: .infinity, minHeight: 34, alignment: .leading)
                            .padding(.horizontal, 12)
                            .background(jarvisCyan.opacity(0.06))
                            .overlay(HUDBrackets())
                    }
                    .buttonStyle(.plain)
                }
            }
            .padding(.top, 8)
        }
        .padding(.top, 30)
    }

    @ViewBuilder
    private func hudLine(_ line: ChatLine) -> some View {
        switch line.role {
        case .user:
            VStack(alignment: .leading, spacing: 4) {
                Text("YOU ›").font(.system(size: 10, weight: .bold, design: .monospaced)).foregroundStyle(.secondary)
                Text(line.text).font(.system(size: 14)).textSelection(.enabled)
            }
        case .bot:
            VStack(alignment: .leading, spacing: 4) {
                Text("HELM ›").font(.system(size: 10, weight: .bold, design: .monospaced)).foregroundStyle(jarvisCyan)
                Text(line.text).font(.system(size: 15)).textSelection(.enabled)
            }
            .padding(.leading, 10)
            .overlay(alignment: .leading) { Rectangle().fill(jarvisCyan).frame(width: 2) }
        case .activity:
            Text(line.text)
                .font(.system(size: 11, design: .monospaced))
                .foregroundStyle(jarvisCyan.opacity(0.7))
                .textSelection(.enabled)
        case .error:
            Label(line.text, systemImage: "exclamationmark.triangle.fill")
                .font(.system(size: 12, design: .monospaced))
                .foregroundStyle(.orange)
                .textSelection(.enabled)
        }
    }
}

/// Permissions/MCP inside the HUD window.
private struct ChatRootViewAdvanced: View {
    @Binding var showAdvanced: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            Button { showAdvanced = false } label: {
                Label("Back", systemImage: "chevron.left")
            }
            .buttonStyle(.plain)
            .foregroundStyle(jarvisCyan)
            .padding(20)
            AssistantPanel().frame(maxWidth: 520)
        }
    }
}

private struct HUDApproval: View {
    @EnvironmentObject private var chat: ChatController
    let action: String

    var body: some View {
        HStack(spacing: 12) {
            Image(systemName: "hand.raised.fill").foregroundStyle(.orange)
            Text("Authorise: \(action)?")
                .font(.system(size: 12, weight: .semibold, design: .monospaced))
            Spacer()
            Button("Stop") { chat.reject() }
            Button("Allow all") { chat.approve(all: true) }
            Button("Allow") { chat.approve() }
                .buttonStyle(.borderedProminent)
                .tint(jarvisCyan)
                .keyboardShortcut(.return, modifiers: [.command])
        }
        .controlSize(.small)
        .padding(12)
        .background(Color.orange.opacity(0.10))
        .overlay(HUDBrackets(color: .orange))
        .padding(.horizontal, 24)
        .padding(.bottom, 8)
    }
}

private struct HUDInput: View {
    @EnvironmentObject private var chat: ChatController

    var body: some View {
        HStack(spacing: 12) {
            Button { chat.toggleDictation() } label: {
                Image(systemName: chat.isListening && chat.dictating ? "waveform" : "mic.fill")
                    .font(.system(size: 13, weight: .semibold))
                    .foregroundStyle(chat.isListening && chat.dictating ? .white : jarvisCyan)
                    .frame(width: 28, height: 28)
                    .background(chat.isListening && chat.dictating ? Color.red : jarvisCyan.opacity(0.12), in: Circle())
            }
            .buttonStyle(.plain)
            .help("Dictate into the box (Control+Shift+D)")
            .accessibilityLabel(chat.isListening && chat.dictating ? "Stop dictation" : "Dictate")
            Text("›").font(.system(size: 16, weight: .bold, design: .monospaced)).foregroundStyle(jarvisCyan)
            TextField("Enter a command…", text: $chat.draft, axis: .vertical)
                .textFieldStyle(.plain)
                .font(.system(size: 14, design: .monospaced))
                .lineLimit(1...4)
                .onSubmit { chat.send() }
                .disabled(chat.isThinking)
            if chat.isThinking {
                Button("ABORT") { chat.stop() }
                    .buttonStyle(.plain)
                    .font(.system(size: 11, weight: .bold, design: .monospaced))
                    .foregroundStyle(.red)
            } else {
                Button("AGENT") { chat.dispatchDraft() }
                    .buttonStyle(.plain)
                    .font(.system(size: 11, weight: .bold, design: .monospaced))
                    .foregroundStyle(AgentCursor.colors["Agent 1"] ?? jarvisCyan)
                    .disabled(chat.draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                    .help("Run this in the background on one of 3 agents")
                Button("SEND") { chat.send() }
                    .buttonStyle(.plain)
                    .font(.system(size: 11, weight: .bold, design: .monospaced))
                    .foregroundStyle(jarvisCyan)
                    .disabled(chat.draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
            }
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 13)
        .background(jarvisCyan.opacity(0.05))
        .overlay(HUDBrackets())
        .padding(24)
    }
}

/// Corner brackets that give panels the HUD frame look.
private struct HUDBrackets: View {
    var color: Color = jarvisCyan

    var body: some View {
        GeometryReader { geo in
            let w = geo.size.width, h = geo.size.height, l: CGFloat = 10
            Path { p in
                p.move(to: .init(x: 0, y: l)); p.addLine(to: .zero); p.addLine(to: .init(x: l, y: 0))
                p.move(to: .init(x: w - l, y: 0)); p.addLine(to: .init(x: w, y: 0)); p.addLine(to: .init(x: w, y: l))
                p.move(to: .init(x: w, y: h - l)); p.addLine(to: .init(x: w, y: h)); p.addLine(to: .init(x: w - l, y: h))
                p.move(to: .init(x: l, y: h)); p.addLine(to: .init(x: 0, y: h)); p.addLine(to: .init(x: 0, y: h - l))
            }
            .stroke(color.opacity(0.8), lineWidth: 1.5)
            Rectangle().strokeBorder(color.opacity(0.15), lineWidth: 1)
        }
        .allowsHitTesting(false)
    }
}

private struct GridBackdrop: View {
    var body: some View {
        Canvas { context, size in
            var path = Path()
            stride(from: 0, through: size.width, by: 32).forEach { x in
                path.move(to: .init(x: x, y: 0)); path.addLine(to: .init(x: x, y: size.height))
            }
            stride(from: 0, through: size.height, by: 32).forEach { y in
                path.move(to: .init(x: 0, y: y)); path.addLine(to: .init(x: size.width, y: y))
            }
            context.stroke(path, with: .color(jarvisCyan.opacity(0.06)), lineWidth: 0.5)
        }
        .allowsHitTesting(false)
    }
}

/// Arc-reactor: counter-rotating segmented rings around a glowing core.
private struct Reactor: View {
    let active: Bool
    let listening: Bool

    var body: some View {
        TimelineView(.animation) { context in
            let t = context.date.timeIntervalSinceReferenceDate
            let speed = active ? 2.5 : 0.6
            let tint = listening ? Color.red : jarvisCyan
            ZStack {
                Circle().fill(tint.opacity(0.18)).blur(radius: 40)
                ring(dash: [2, 6], width: 10, inset: 0, angle: t * 12 * speed, color: tint.opacity(0.5))
                ring(dash: [60, 18], width: 3, inset: 22, angle: -t * 30 * speed, color: tint)
                ring(dash: [8, 4], width: 6, inset: 40, angle: t * 45 * speed, color: tint.opacity(0.7))
                ring(dash: [120, 30, 20, 30], width: 2, inset: 62, angle: -t * 20 * speed, color: tint.opacity(0.9))
                Circle()
                    .fill(RadialGradient(colors: [.white, tint, tint.opacity(0.2)], center: .center, startRadius: 2, endRadius: 46))
                    .padding(78)
                    .scaleEffect(1 + 0.05 * sin(t * (active ? 6 : 2)))
                    .shadow(color: tint, radius: 24)
            }
        }
        .accessibilityHidden(true)
    }

    private func ring(dash: [CGFloat], width: CGFloat, inset: CGFloat, angle: Double, color: Color) -> some View {
        Circle()
            .stroke(color, style: StrokeStyle(lineWidth: width, dash: dash))
            .padding(inset + width / 2)
            .rotationEffect(.degrees(angle))
    }
}

// MARK: - AI provider settings

struct ProviderSettingsView: View {
    @EnvironmentObject private var chat: ChatController
    @State private var keyDraft = ""
    @State private var keyStatus: String?

    var body: some View {
        let option = ProviderOption.find(chat.provider)
        VStack(alignment: .leading, spacing: 14) {
            Text("CHOOSE HELM'S BRAIN")
                .font(.system(size: 11, weight: .semibold, design: .monospaced))
                .tracking(2)
                .foregroundStyle(.secondary)
            LazyVGrid(columns: [GridItem(.flexible()), GridItem(.flexible()), GridItem(.flexible())], spacing: 8) {
                ForEach(ProviderOption.all) { item in
                    Button {
                        chat.provider = item.id
                        keyDraft = ""
                        keyStatus = nil
                    } label: {
                        VStack(spacing: 6) {
                            Image(systemName: item.symbol).font(.system(size: 18))
                            Text(item.label).font(.system(size: 11, weight: .medium))
                            if item.id != "ollama" {
                                Text(chat.savedKeys.contains(item.id) ? "key saved" : "needs key")
                                    .font(.system(size: 9, design: .monospaced))
                                    .foregroundStyle(chat.savedKeys.contains(item.id) ? .green : .secondary)
                            } else {
                                Text("free · private").font(.system(size: 9, design: .monospaced)).foregroundStyle(.secondary)
                            }
                        }
                        .frame(maxWidth: .infinity, minHeight: 74)
                        .background(chat.provider == item.id ? jarvisCyan.opacity(0.14) : Color.white.opacity(0.04))
                        .overlay(HUDBrackets(color: chat.provider == item.id ? jarvisCyan : .gray.opacity(0.5)))
                        .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                    .accessibilityAddTraits(chat.provider == item.id ? .isSelected : [])
                }
            }

            HStack {
                Text("Model").font(.system(size: 12)).frame(width: 60, alignment: .leading)
                TextField(option.defaultModel, text: Binding(
                    get: { chat.model(for: option.id) },
                    set: { chat.setModel($0, for: option.id) }
                ))
                .textFieldStyle(.roundedBorder)
                .font(.system(size: 12, design: .monospaced))
            }

            if let keyURL = option.keyURL, let url = URL(string: keyURL) {
                VStack(alignment: .leading, spacing: 8) {
                    HStack {
                        Text("API key").font(.system(size: 12)).frame(width: 60, alignment: .leading)
                        SecureField(chat.savedKeys.contains(option.id) ? "•••••••• (saved — paste to replace)" : "Paste your \(option.label) key", text: $keyDraft)
                            .textFieldStyle(.roundedBorder)
                            .onSubmit { save(option) }
                        Button("Save") { save(option) }
                            .disabled(keyDraft.trimmingCharacters(in: .whitespaces).isEmpty)
                    }
                    HStack {
                        Link("Get a \(option.label) key ↗", destination: url).font(.system(size: 11))
                        Spacer()
                        if let keyStatus {
                            Text(keyStatus).font(.system(size: 11)).foregroundStyle(.green)
                        }
                        if chat.savedKeys.contains(option.id) {
                            Button("Remove key", role: .destructive) {
                                chat.removeKey(for: option.id)
                                keyStatus = nil
                            }
                            .font(.system(size: 11))
                        }
                    }
                    Text("Stored in your Mac's Keychain. Chats go to \(option.label) when this brain is selected.")
                        .font(.system(size: 10))
                        .foregroundStyle(.secondary)
                }
            } else {
                Text("Runs entirely on this Mac with Ollama. Nothing leaves your computer.")
                    .font(.system(size: 11))
                    .foregroundStyle(.secondary)
            }

            HStack(spacing: 10) {
                Button {
                    chat.checkKey()
                } label: {
                    Label(option.id == "ollama" ? "Check Ollama" : "Test key", systemImage: "bolt.horizontal.circle")
                }
                .disabled(chat.isCheckingKey || (option.id != "ollama" && !chat.savedKeys.contains(option.id)))
                if chat.isCheckingKey {
                    ProgressView().controlSize(.small)
                } else if let result = chat.keyCheck {
                    Label(result.message, systemImage: result.ok ? "checkmark.seal.fill" : "xmark.octagon.fill")
                        .font(.system(size: 11))
                        .foregroundStyle(result.ok ? .green : .orange)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
        }
        .onChange(of: chat.provider) { _ in chat.keyCheck = nil }
    }

    private func save(_ option: ProviderOption) {
        if chat.saveKey(keyDraft, for: option.id) {
            keyDraft = ""
            keyStatus = "Saved to Keychain ✓"
        } else {
            keyStatus = nil
        }
    }
}

// MARK: - Abilities

private struct Ability: Identifiable {
    let id: String
    let title: String
    let detail: String
    let symbol: String
    let tools: [String]

    static let all = [
        Ability(id: "screen", title: "Screen control", detail: "Clicks, types, reads and navigates apps for you", symbol: "cursorarrow.click.2", tools: ["control_screen", "resume_task", "click_in_app", "menu_in_app", "type_in_app", "keys_in_app", "read_screen", "new_document", "paste_into_app", "send_message", "read_chat", "clipboard", "window_control", "drag_in_app"]),
        Ability(id: "web", title: "Web, YouTube & TikTok", detail: "Opens websites, searches the web and finds video examples", symbol: "magnifyingglass", tools: ["open_url", "web_search", "ask_chatgpt", "watch_video"]),
        Ability(id: "apps", title: "Applications", detail: "Opens apps and manages installs with confirmation", symbol: "square.grid.2x2", tools: ["open_app", "install_app", "uninstall_app"]),
        Ability(id: "files", title: "Files & folders", detail: "List, read, copy, move and delete files in your home folder — deletes and moves always ask first", symbol: "folder", tools: ["file_operations"]),
        Ability(id: "media", title: "Music & volume", detail: "Plays, pauses and skips in Spotify or Music", symbol: "music.note", tools: ["media_control", "set_volume"]),
        Ability(id: "status", title: "Time & battery", detail: "Reads the current date, time and battery level", symbol: "battery.75percent", tools: ["system_status"]),
    ]
}

struct AbilitiesView: View {
    @EnvironmentObject private var chat: ChatController

    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            Text("INTERFACE")
                .font(.system(size: 11, weight: .semibold, design: .monospaced))
                .tracking(2)
                .foregroundStyle(.secondary)
            HStack(spacing: 10) {
                styleCard("hud", "Full HUD window", "Big Jarvis screen with the reactor. The menu bar icon just opens it.", "macwindow")
                styleCard("menubar", "Menu bar popup", "Compact chat that drops down from the menu bar. No big window.", "menubar.rectangle")
            }
            Text("ABILITIES")
                .font(.system(size: 11, weight: .semibold, design: .monospaced))
                .tracking(2)
                .foregroundStyle(.secondary)
            LazyVGrid(columns: [GridItem(.flexible()), GridItem(.flexible())], spacing: 10) {
                ForEach(Ability.all) { ability in
                    let enabled = chat.isEnabled(ability.tools)
                    Button {
                        chat.setEnabled(ability.tools, !enabled)
                    } label: {
                        HStack(alignment: .top, spacing: 12) {
                            Image(systemName: ability.symbol)
                                .font(.system(size: 18))
                                .foregroundStyle(enabled ? jarvisCyan : .secondary)
                                .frame(width: 26)
                            VStack(alignment: .leading, spacing: 3) {
                                Text(ability.title).font(.system(size: 13, weight: .semibold))
                                Text(ability.detail).font(.system(size: 11)).foregroundStyle(.secondary)
                                    .fixedSize(horizontal: false, vertical: true)
                            }
                            Spacer(minLength: 0)
                            Image(systemName: enabled ? "checkmark.circle.fill" : "circle")
                                .foregroundStyle(enabled ? jarvisCyan : .secondary)
                        }
                        .padding(12)
                        .frame(maxWidth: .infinity, minHeight: 70, alignment: .topLeading)
                        .background(enabled ? jarvisCyan.opacity(0.08) : Color.white.opacity(0.03))
                        .overlay(HUDBrackets(color: enabled ? jarvisCyan : .gray.opacity(0.4)))
                        .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                    .accessibilityLabel("\(ability.title), \(enabled ? "on" : "off")")
                }
            }
            VStack(alignment: .leading, spacing: 10) {
                Text("BEHAVIOUR")
                    .font(.system(size: 11, weight: .semibold, design: .monospaced))
                    .tracking(2)
                    .foregroundStyle(.secondary)
                Picker("Browser for searches", selection: $chat.browser) {
                    Text("Google Chrome").tag("chrome")
                    Text("Safari").tag("safari")
                }
                .pickerStyle(.segmented)
                .frame(maxWidth: 420)
                Toggle("Voice shortcut — hold Control+Option to talk", isOn: $chat.voiceHotkey)
                Toggle("Speak replies out loud", isOn: $chat.speakReplies)
                Toggle("Cursor buddy — a little Helm next to your mouse that shows what's happening", isOn: Binding(
                    get: { AgentCursor.shared.buddyEnabled }, set: { AgentCursor.shared.buddyEnabled = $0 }
                ))
                Toggle("Full autonomy — act on the screen without asking first", isOn: Binding(
                    get: { !chat.askBeforeActing }, set: { chat.askBeforeActing = !$0 }
                ))
                Toggle("Let the cloud brain drive the screen (sends screenshots to it; much more accurate)", isOn: $chat.cloudVision)
                    .disabled(chat.provider == "ollama")
            }
            .font(.system(size: 12))
        }
    }
}

extension AbilitiesView {
    fileprivate func styleCard(_ id: String, _ title: String, _ detail: String, _ symbol: String) -> some View {
        let selected = chat.interfaceStyle == id
        return Button {
            chat.interfaceStyle = id
        } label: {
            HStack(alignment: .top, spacing: 12) {
                Image(systemName: symbol).font(.system(size: 20)).foregroundStyle(selected ? jarvisCyan : .secondary).frame(width: 28)
                VStack(alignment: .leading, spacing: 3) {
                    Text(title).font(.system(size: 13, weight: .semibold))
                    Text(detail).font(.system(size: 11)).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                }
                Spacer(minLength: 0)
                Image(systemName: selected ? "largecircle.fill.circle" : "circle").foregroundStyle(selected ? jarvisCyan : .secondary)
            }
            .padding(12)
            .frame(maxWidth: .infinity, minHeight: 76, alignment: .topLeading)
            .background(selected ? jarvisCyan.opacity(0.10) : Color.white.opacity(0.03))
            .overlay(HUDBrackets(color: selected ? jarvisCyan : .gray.opacity(0.4)))
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityAddTraits(selected ? .isSelected : [])
    }
}

// MARK: - Permissions

private enum PermissionState {
    case granted
    case needsPermission
    case promptOnUse
    case limited
    case unavailable
    case disabled

    var label: String {
        switch self {
        case .granted: "Available"
        case .needsPermission: "Needs permission"
        case .promptOnUse: "macOS prompt on use"
        case .limited: "Limited access"
        case .unavailable: "Not implemented"
        case .disabled: "Turned off"
        }
    }

    var color: Color {
        switch self {
        case .granted: .green
        case .needsPermission: .orange
        case .promptOnUse, .limited: .yellow
        case .unavailable, .disabled: .secondary
        }
    }
}

private struct PermissionItem: Identifiable {
    let id: String
    let symbol: String
    let title: String
    let detail: String
    let state: PermissionState
    let canRequest: Bool
}

private enum PermissionManager {
    /// Apple Events (Automation) has no CGPreflight-style check; probe with a harmless, read-only
    /// script. The first call prompts the user; a denied app answers with error -1743. The
    /// Permissions tab re-renders every second (TimelineView), so this result is cached briefly
    /// rather than spawning osascript on every tick.
    private static var cachedAutomationAllowed: Bool?
    private static var lastAutomationCheck = Date.distantPast

    private static func automationAllowed() -> Bool {
        let now = Date()
        if let cached = cachedAutomationAllowed, now.timeIntervalSince(lastAutomationCheck) < 5 {
            return cached
        }
        lastAutomationCheck = now
        let script = Process()
        script.executableURL = URL(fileURLWithPath: "/usr/bin/osascript")
        script.arguments = ["-e", "tell application \"Finder\" to return name"]
        let stderr = Pipe()
        script.standardOutput = Pipe()
        script.standardError = stderr
        let result: Bool
        do {
            try script.run()
            script.waitUntilExit()
            if script.terminationStatus == 0 {
                result = true
            } else {
                let errorText = String(decoding: stderr.fileHandleForReading.readDataToEndOfFile(), as: UTF8.self)
                result = !errorText.contains("-1743") && !errorText.localizedCaseInsensitiveContains("not allowed")
            }
        } catch {
            result = false  // osascript itself could not run; treat as unknown/denied rather than crash
        }
        cachedAutomationAllowed = result
        return result
    }

    /// Forces the next automationAllowed() call to re-probe instead of serving the cache,
    /// so pressing "Allow" reflects the user's answer immediately.
    private static func invalidateAutomationCache() {
        lastAutomationCheck = .distantPast
    }

    private static let desktopTools: Set<String> = [
        "control_screen", "resume_task", "click_in_app", "menu_in_app", "type_in_app", "keys_in_app",
        "read_screen", "new_document", "paste_into_app", "send_message", "read_chat", "clipboard",
    ]

    static func items(
        disabledTools: Set<String>,
        voiceHotkey: Bool,
        emergencyStopShortcutAvailable: Bool
    ) -> [PermissionItem] {
        let screenEnabled = !disabledTools.contains("control_screen")
        let desktopEnabled = !desktopTools.isSubset(of: disabledTools)
        let browserEnabled = !Set(["open_url", "web_search", "ask_chatgpt", "watch_video"]).isSubset(of: disabledTools)
        let appsEnabled = !Set(["open_app", "install_app", "uninstall_app"]).isSubset(of: disabledTools)
        let microphoneAllowed = AVCaptureDevice.authorizationStatus(for: .audio) == .authorized
        let speechAllowed = SFSpeechRecognizer.authorizationStatus() == .authorized
        let accessibilityAllowed = AXIsProcessTrusted()

        return [
            PermissionItem(
                id: "microphone", symbol: "mic.fill", title: "Microphone",
                detail: "Used only when you start voice input.",
                state: microphoneAllowed ? .granted : .needsPermission,
                canRequest: true
            ),
            PermissionItem(
                id: "speech", symbol: "text.bubble", title: "Speech recognition",
                detail: "Voice transcription is configured for on-device recognition.",
                state: speechAllowed ? .granted : .needsPermission,
                canRequest: true
            ),
            PermissionItem(
                id: "input-monitoring", symbol: "keyboard", title: "Input Monitoring",
                detail: "Required for the Control+Option voice shortcut.",
                state: voiceHotkey
                    ? (CGPreflightListenEventAccess() ? .granted : .needsPermission)
                    : .disabled,
                canRequest: voiceHotkey
            ),
            PermissionItem(
                id: "screen-recording", symbol: "rectangle.dashed.badge.record", title: "Screen Recording",
                detail: "Required to capture the screen for screen-control tasks.",
                state: screenEnabled
                    ? (CGPreflightScreenCaptureAccess() ? .granted : .needsPermission)
                    : .disabled,
                canRequest: screenEnabled
            ),
            PermissionItem(
                id: "keyboard", symbol: "keyboard", title: "Keyboard",
                detail: "Accessibility access is used for in-app typing and shortcuts.",
                state: desktopEnabled ? (accessibilityAllowed ? .granted : .needsPermission) : .disabled,
                canRequest: desktopEnabled
            ),
            PermissionItem(
                id: "mouse", symbol: "computermouse", title: "Mouse",
                detail: "Accessibility access is used for named controls; coordinates are a fallback.",
                state: desktopEnabled ? (accessibilityAllowed ? .granted : .needsPermission) : .disabled,
                canRequest: desktopEnabled
            ),
            PermissionItem(
                id: "browser", symbol: "globe", title: "Browser",
                detail: browserEnabled
                    ? "Search opens your browser. To read results, enable Allow JavaScript from Apple Events in its developer settings; macOS may prompt. Login and CAPTCHA are manual."
                    : "Browser search is turned off in Abilities.",
                state: browserEnabled ? .promptOnUse : .disabled,
                canRequest: false
            ),
            PermissionItem(
                id: "automation", symbol: "bolt.horizontal.circle", title: "Automation (Apple Events)",
                detail: "Needed to script Finder, System Events, Safari and Chrome — used for opening apps correctly, reading browser search results, and verifying chat windows before sending.",
                state: automationAllowed() ? .granted : .needsPermission,
                canRequest: true
            ),
            PermissionItem(
                id: "files", symbol: "folder", title: "Files",
                detail: "No general file tools are built in. A configured MCP filesystem server has separate access; only trust servers you configure.",
                state: .limited,
                canRequest: false
            ),
            PermissionItem(
                id: "applications", symbol: "square.grid.2x2", title: "Applications",
                detail: appsEnabled
                    ? "App launch and install tools are enabled; install/removal still needs confirmation."
                    : "App launching is turned off in Abilities.",
                state: appsEnabled ? .granted : .disabled,
                canRequest: false
            ),
            PermissionItem(
                id: "system-settings", symbol: "gearshape", title: "System Settings",
                detail: "Helm can open Privacy & Security settings, but does not change settings for you.",
                state: .limited,
                canRequest: false
            ),
            PermissionItem(
                id: "administration", symbol: "lock.shield", title: "Administrative operations",
                detail: "No administrator or sudo operations are implemented or requested.",
                state: .unavailable,
                canRequest: false
            ),
            PermissionItem(
                id: "emergency-stop", symbol: "stop.circle", title: "Emergency stop",
                detail: "Control+Shift+Escape stops the chat task, background agents, and speech.",
                state: emergencyStopShortcutAvailable ? .granted : .unavailable,
                canRequest: false
            ),
        ]
    }

    static func request(_ id: String) {
        switch id {
        case "microphone":
            if AVCaptureDevice.authorizationStatus(for: .audio) == .denied {
                openPrivacySettings("Privacy_Microphone")
            } else {
                AVCaptureDevice.requestAccess(for: .audio) { _ in }
            }
        case "speech":
            if SFSpeechRecognizer.authorizationStatus() == .denied {
                openPrivacySettings("Privacy_SpeechRecognition")
            } else {
                SFSpeechRecognizer.requestAuthorization { _ in }
            }
        case "input-monitoring":
            _ = CGRequestListenEventAccess()
        case "screen-recording":
            _ = CGRequestScreenCaptureAccess()
        case "keyboard", "mouse":
            let options = [kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String: true] as CFDictionary
            _ = AXIsProcessTrustedWithOptions(options)
        case "automation":
            invalidateAutomationCache()
            if !automationAllowed() {
                // Already decided (denied) — the prompt won't reappear; send the user to Settings.
                openPrivacySettings("Privacy_Automation")
            }
        default:
            break
        }
    }

    static func openPrivacySettings(_ pane: String = "Privacy") {
        guard let url = URL(string: "x-apple.systempreferences:com.apple.preference.security?\(pane)") else {
            return
        }
        NSWorkspace.shared.open(url)
    }
}

struct PermissionsView: View {
    @EnvironmentObject private var chat: ChatController

    var body: some View {
        TimelineView(.periodic(from: .now, by: 1)) { _ in
            VStack(alignment: .leading, spacing: 10) {
                ForEach(PermissionManager.items(
                    disabledTools: chat.disabledTools,
                    voiceHotkey: chat.voiceHotkey,
                    emergencyStopShortcutAvailable: chat.emergencyStopShortcutAvailable
                )) { item in
                    permissionRow(item)
                }
                if chat.isEnabled(["media_control"]) {
                    Label("Music control: macOS asks once, the first time you ask Helm to play something.", systemImage: "info.circle")
                        .font(.system(size: 11))
                        .foregroundStyle(.secondary)
                        .padding(.top, 4)
                }
                if chat.isEnabled(["control_screen"]) && !CGPreflightScreenCaptureAccess() {
                    VStack(alignment: .leading, spacing: 8) {
                        Label("Already switched on in System Settings but still not allowed here?", systemImage: "exclamationmark.triangle.fill")
                            .font(.system(size: 12, weight: .semibold))
                            .foregroundStyle(.orange)
                        Text("macOS only applies Screen Recording after Helm restarts. If it is still off after a restart, the switch belongs to an older build: in Screen & System Audio Recording select LocalDesktopAssistant, press −, then click Allow here again.")
                            .font(.system(size: 11))
                            .foregroundStyle(.secondary)
                            .fixedSize(horizontal: false, vertical: true)
                        Button("Restart Helm") { ChatController.relaunch() }
                    }
                    .padding(12)
                    .background(Color.orange.opacity(0.08))
                }
                Button("Open Privacy & Security settings") {
                    PermissionManager.openPrivacySettings()
                }
                .font(.system(size: 11))
                .padding(.top, 4)
            }
        }
    }

    private func permissionRow(_ item: PermissionItem) -> some View {
        HStack(spacing: 14) {
            Image(systemName: item.symbol).font(.system(size: 17)).foregroundStyle(item.state.color).frame(width: 26)
            VStack(alignment: .leading, spacing: 2) {
                Text(item.title).font(.system(size: 13, weight: .semibold))
                Text(item.detail).font(.system(size: 11)).foregroundStyle(.secondary)
            }
            Spacer()
            Text(item.state.label)
                .font(.system(size: 10, weight: .medium))
                .foregroundStyle(item.state.color)
            if item.canRequest && item.state == .needsPermission {
                Button("Allow") { PermissionManager.request(item.id) }
            }
        }
        .padding(11)
        .background(Color.white.opacity(0.03))
        .overlay(HUDBrackets(color: item.state.color.opacity(0.7)))
    }
}

// MARK: - First launch

/// Shown only on a genuine first run (see ChatController.isFirstRun) or via "Run setup again".
struct OnboardingView: View {
    @EnvironmentObject private var chat: ChatController
    @State private var step = 0

    private let steps = [
        ("Welcome", "Meet Helm"),
        ("Brain", "AI model & API key"),
        ("Abilities", "What it can do"),
        ("Permissions", "What macOS allows"),
        ("Launch", "All systems go"),
    ]

    var body: some View {
        HStack(spacing: 0) {
            rail.frame(width: 260)
            Rectangle().fill(jarvisCyan.opacity(0.15)).frame(width: 1)
            VStack(spacing: 0) {
                ScrollView {
                    VStack(alignment: .leading, spacing: 18) {
                        Text(title).font(.system(size: 30, weight: .light))
                        Text(subtitle).font(.system(size: 13)).foregroundStyle(.secondary)
                            .fixedSize(horizontal: false, vertical: true)
                        content.padding(.top, 6)
                    }
                    .frame(maxWidth: 640, alignment: .leading)
                    .padding(40)
                    .frame(maxWidth: .infinity)
                }
                .id(step)
                .transition(.asymmetric(insertion: .move(edge: .trailing).combined(with: .opacity), removal: .opacity))
                navigation
            }
        }
        .animation(.easeInOut(duration: 0.25), value: step)
    }

    private var rail: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(spacing: 10) {
                Orb(active: false, size: 26)
                Text("HELM")
                    .font(.system(size: 12, weight: .bold, design: .monospaced))
                    .tracking(3)
                    .foregroundStyle(jarvisCyan)
                    .fixedSize()
            }
            .padding(.bottom, 34)
            ForEach(Array(steps.enumerated()), id: \.offset) { index, item in
                HStack(spacing: 12) {
                    ZStack {
                        Circle()
                            .strokeBorder(index <= step ? jarvisCyan : Color.white.opacity(0.2), lineWidth: 1.5)
                            .background(Circle().fill(index < step ? jarvisCyan : .clear))
                        if index < step {
                            Image(systemName: "checkmark").font(.system(size: 10, weight: .bold)).foregroundStyle(.black)
                        } else {
                            Text("\(index + 1)").font(.system(size: 11, weight: .semibold, design: .monospaced))
                                .foregroundStyle(index == step ? jarvisCyan : .secondary)
                        }
                    }
                    .frame(width: 24, height: 24)
                    VStack(alignment: .leading, spacing: 1) {
                        Text(item.0).font(.system(size: 13, weight: index == step ? .semibold : .regular))
                            .foregroundStyle(index <= step ? .primary : .secondary)
                        Text(item.1).font(.system(size: 10)).foregroundStyle(.secondary)
                    }
                }
                .padding(.vertical, 10)
                .contentShape(Rectangle())
                .onTapGesture { if index < step { step = index } }
            }
            Spacer()
            Text("You can change all of this later in Settings.")
                .font(.system(size: 10))
                .foregroundStyle(.secondary)
        }
        .padding(28)
        .background(Color.black.opacity(0.25))
    }

    private var title: String {
        ["Meet Helm", "Choose its brain", "Pick its abilities", "Give it permission", "All systems online"][step]
    }

    private var subtitle: String {
        [
            "A Jarvis-style assistant that talks, listens, searches and works your Mac for you.",
            "Run free and private on this Mac with Ollama, or plug in a cloud AI with your own API key. Cloud models are smarter and much better at controlling the screen.",
            "Turn on only what you want. Anything off is never offered to the AI.",
            "macOS asks you to approve each one. Rows turn green as soon as you allow them.",
            "Here's how Helm is set up. Hold Control+Option and say something, or type below.",
        ][step]
    }

    @ViewBuilder
    private var content: some View {
        switch step {
        case 0: welcome
        case 1: ProviderSettingsView()
        case 2: AbilitiesView()
        case 3: PermissionsView()
        default: summary
        }
    }

    private var welcome: some View {
        HStack(alignment: .center, spacing: 34) {
            Reactor(active: true, listening: false).frame(width: 200, height: 200)
            VStack(alignment: .leading, spacing: 12) {
                feature("waveform", "Talk to it", "Hold Control+Option, or click the reactor")
                feature("cursorarrow.click.2", "Works your Mac", "Opens apps, clicks and types for you")
                feature("magnifyingglass", "Searches for you", "The web, YouTube and TikTok")
                feature("music.note", "Runs the room", "Music, volume, time and battery")
                feature("key", "Your choice of brain", "Local, Claude, ChatGPT, Gemini or OpenRouter")
            }
        }
    }

    private func feature(_ symbol: String, _ title: String, _ detail: String) -> some View {
        HStack(spacing: 12) {
            Image(systemName: symbol).foregroundStyle(jarvisCyan).frame(width: 22)
            VStack(alignment: .leading, spacing: 1) {
                Text(title).font(.system(size: 13, weight: .semibold))
                Text(detail).font(.system(size: 11)).foregroundStyle(.secondary)
            }
        }
    }

    private var summary: some View {
        let enabled = Ability.all.filter { chat.isEnabled($0.tools) }
        return VStack(alignment: .leading, spacing: 12) {
            summaryRow("cpu", "Brain", chat.brainLabel)
            summaryRow("square.stack.3d.up", "Abilities", enabled.isEmpty ? "Chat only" : enabled.map(\.title).joined(separator: ", "))
            summaryRow("hand.raised", "Screen actions", chat.askBeforeActing ? "Asks before each step" : "Full autonomy")
            summaryRow("waveform", "Voice", chat.voiceHotkey ? "Hold Control+Option" : "Mic button only")
            Text("TRY SAYING")
                .font(.system(size: 11, weight: .semibold, design: .monospaced))
                .tracking(2)
                .foregroundStyle(.secondary)
                .padding(.top, 10)
            ForEach(["“Find TikTok videos about latte art”", "“Open Safari and go to youtube.com”", "“Play some music and set the volume to 30”"], id: \.self) {
                Text($0).font(.system(size: 13, design: .monospaced)).foregroundStyle(jarvisCyan)
            }
        }
    }

    private func summaryRow(_ symbol: String, _ title: String, _ value: String) -> some View {
        HStack(alignment: .top, spacing: 12) {
            Image(systemName: symbol).foregroundStyle(jarvisCyan).frame(width: 22)
            Text(title).font(.system(size: 13, weight: .semibold)).frame(width: 110, alignment: .leading)
            Text(value).font(.system(size: 13)).foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.white.opacity(0.03))
        .overlay(HUDBrackets())
    }

    private var needsKey: Bool { chat.provider != "ollama" && !chat.savedKeys.contains(chat.provider) }

    private var navigation: some View {
        HStack {
            if step > 0 {
                Button("Back") { step -= 1 }
            }
            Spacer()
            if step == 1 && needsKey {
                Text("Save a key, or pick Local to continue.")
                    .font(.system(size: 11))
                    .foregroundStyle(.orange)
            }
            Button(step == steps.count - 1 ? "Launch Helm" : "Continue") {
                if step == steps.count - 1 { chat.completeSetup() } else { step += 1 }
            }
            .buttonStyle(.borderedProminent)
            .tint(jarvisCyan)
            .keyboardShortcut(.defaultAction)
            .disabled(step == 1 && needsKey)
        }
        .padding(.horizontal, 32)
        .padding(.vertical, 18)
        .background(Color.black.opacity(0.2))
    }
}

struct WhatsAppSettingsView: View {
    @EnvironmentObject private var chat: ChatController
    @AppStorage("whatsapp.phone-number-id") private var phoneNumberID = ""
    @AppStorage("whatsapp.owner-number") private var ownerNumber = ""
    @AppStorage("whatsapp.api-version") private var apiVersion = "v23.0"
    @AppStorage("whatsapp.notify-local-tasks") private var notifyLocalTasks = false
    @AppStorage("whatsapp.auto-start") private var autoStart = false
    @State private var accessTokenDraft = ""
    @State private var appSecretDraft = ""
    @State private var verifyTokenDraft = ""
    @State private var saveStatus: String?

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text("WHATSAPP CLOUD API")
                .font(.system(size: 11, weight: .semibold, design: .monospaced))
                .tracking(2)
                .foregroundStyle(.secondary)
            Text("Link your WhatsApp Business Cloud API. Messages reach this Mac through a secure public tunnel; the Mac and Helm must stay awake and online.")
                .font(.system(size: 12))
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)

            HStack(spacing: 10) {
                TextField("Phone Number ID", text: $phoneNumberID)
                    .textFieldStyle(.roundedBorder)
                TextField("Your WhatsApp number, E.164 digits only", text: $ownerNumber)
                    .textFieldStyle(.roundedBorder)
            }
            HStack(spacing: 10) {
                SecureField(KeyStore.hasSecret("whatsapp.access-token") ? "Access token saved (enter to replace)" : "Permanent access token", text: $accessTokenDraft)
                    .textFieldStyle(.roundedBorder)
                Button("Save") { saveSecret(accessTokenDraft, key: "whatsapp.access-token", label: "Access token") }
                    .disabled(accessTokenDraft.isEmpty)
            }
            HStack(spacing: 10) {
                SecureField(KeyStore.hasSecret("whatsapp.app-secret") ? "App secret saved (enter to replace)" : "Meta app secret", text: $appSecretDraft)
                    .textFieldStyle(.roundedBorder)
                Button("Save") { saveSecret(appSecretDraft, key: "whatsapp.app-secret", label: "App secret") }
                    .disabled(appSecretDraft.isEmpty)
            }
            HStack(spacing: 10) {
                SecureField(KeyStore.hasSecret("whatsapp.verify-token") ? "Verify token saved (enter to replace)" : "Webhook verify token (24+ letters/digits/_/-)", text: $verifyTokenDraft)
                    .textFieldStyle(.roundedBorder)
                Button("Save") { saveSecret(verifyTokenDraft, key: "whatsapp.verify-token", label: "Verify token") }
                    .disabled(verifyTokenDraft.isEmpty)
            }
            HStack {
                Text("Graph API version")
                    .font(.system(size: 11))
                TextField("v23.0", text: $apiVersion)
                    .textFieldStyle(.roundedBorder)
                    .frame(width: 100)
                Spacer()
                if let saveStatus {
                    Text(saveStatus)
                        .font(.system(size: 11))
                        .foregroundStyle(saveStatus.hasPrefix("Could") ? .orange : .green)
                }
            }
            Toggle("Text me when a task I start on this Mac finishes", isOn: $notifyLocalTasks)
                .font(.system(size: 12))
            Text("Proactive notifications may require an approved WhatsApp message template if you have not messaged the business number in the last 24 hours. Remote command replies are sent in response to your message.")
                .font(.system(size: 10))
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            Toggle("Start automatically when Helm launches", isOn: $autoStart)
                .font(.system(size: 12))

            HStack(spacing: 12) {
                Button {
                    chat.startWhatsAppBridge()
                } label: {
                    Label("Start local webhook", systemImage: "antenna.radiowaves.left.and.right")
                }
                .disabled(!hasRequiredSecrets || phoneNumberID.isEmpty || ownerNumber.isEmpty)
                Button("Stop") { chat.stopWhatsAppBridge() }
                    .disabled(chat.whatsappBridgeStatus == "Stopped" || chat.whatsappBridgeStatus == "Not configured")
                Text(chat.whatsappBridgeStatus)
                    .font(.system(size: 11, design: .monospaced))
                    .foregroundStyle(chat.whatsappBridgeStatus.hasPrefix("Running") ? .green : .secondary)
                    .lineLimit(2)
            }

            GroupBox("Setup") {
                VStack(alignment: .leading, spacing: 8) {
                    Text("1. In Meta for Developers, create a WhatsApp Cloud API app. Save its Phone Number ID, permanent access token, and App Secret above.")
                    Text("2. Save a random 24+ character verify token above. Start the webhook, then run in Terminal: cloudflared tunnel --url http://127.0.0.1:8765")
                    Text("3. In Meta Webhooks, set the callback URL to https://YOUR-TUNNEL-HOST/webhook, enter the same verify token, and subscribe to the WhatsApp messages field.")
                    Text("4. Send a WhatsApp text from your own number. Helm can perform remote tasks; sensitive steps pause and require APPROVE <code> or DENY <code> in WhatsApp.")
                }
                .font(.system(size: 10))
                .fixedSize(horizontal: false, vertical: true)
                .frame(maxWidth: .infinity, alignment: .leading)
            }
            Text("The bridge binds only to 127.0.0.1. Do not expose port 8765 directly; use an HTTPS tunnel. Log: ~/Library/Logs/Helm/whatsapp-bridge.log")
                .font(.system(size: 10, design: .monospaced))
                .foregroundStyle(.secondary)
                .textSelection(.enabled)
        }
    }

    private var hasRequiredSecrets: Bool {
        KeyStore.hasSecret("whatsapp.access-token")
            && KeyStore.hasSecret("whatsapp.app-secret")
            && KeyStore.hasSecret("whatsapp.verify-token")
    }

    private func saveSecret(_ value: String, key: String, label: String) {
        if KeyStore.saveSecret(value.trimmingCharacters(in: .whitespacesAndNewlines), key: key) {
            saveStatus = "\(label) saved in Keychain."
            switch key {
            case "whatsapp.access-token": accessTokenDraft = ""
            case "whatsapp.app-secret": appSecretDraft = ""
            case "whatsapp.verify-token": verifyTokenDraft = ""
            default: break
            }
        } else {
            saveStatus = "Could not save \(label.lowercased()) to Keychain."
        }
    }
}

struct TelegramSettingsView: View {
    @EnvironmentObject private var chat: ChatController
    @AppStorage("telegram.owner-chat-id") private var ownerChatID = ""
    @AppStorage("telegram.auto-start") private var autoStart = false
    @State private var tokenDraft = ""
    @State private var proxyDraft = ""
    @State private var saveStatus: String?

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text("TELEGRAM BOT")
                .font(.system(size: 11, weight: .semibold, design: .monospaced))
                .tracking(2)
                .foregroundStyle(.secondary)
            Text("No phone number, business account or public tunnel needed — the bot polls Telegram directly. The Mac and Helm must stay awake and online.")
                .font(.system(size: 12))
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)

            HStack(spacing: 10) {
                SecureField(KeyStore.hasSecret("telegram.bot-token") ? "Bot token saved (enter to replace)" : "Bot token from @BotFather", text: $tokenDraft)
                    .textFieldStyle(.roundedBorder)
                Button("Save") { saveToken() }
                    .disabled(tokenDraft.isEmpty)
            }
            HStack(spacing: 10) {
                TextField("Owner chat ID (leave empty to pair)", text: $ownerChatID)
                    .textFieldStyle(.roundedBorder)
                if let saveStatus {
                    Text(saveStatus)
                        .font(.system(size: 11))
                        .foregroundStyle(saveStatus.hasPrefix("Could") ? .orange : .green)
                }
            }
            HStack(spacing: 10) {
                SecureField(KeyStore.hasSecret("telegram.proxy-url") ? "Proxy saved (enter to replace)" : "HTTP(S) proxy URL — only if Telegram is blocked here", text: $proxyDraft)
                    .textFieldStyle(.roundedBorder)
                Button("Save") { saveProxy() }
                    .disabled(proxyDraft.isEmpty)
                if KeyStore.hasSecret("telegram.proxy-url") {
                    Button("Clear", role: .destructive) { clearProxy() }
                }
            }
            Text("Leave this empty unless api.telegram.org is unreachable on this network. Point it at a VPN app's local HTTP proxy, or any HTTP(S) CONNECT proxy, e.g. http://host:port or http://user:pass@host:port.")
                .font(.system(size: 10))
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            Toggle("Start automatically when Helm launches", isOn: $autoStart)
                .font(.system(size: 12))

            HStack(spacing: 12) {
                Button {
                    chat.startTelegramBridge()
                } label: {
                    Label("Start bridge", systemImage: "paperplane")
                }
                .disabled(!KeyStore.hasSecret("telegram.bot-token"))
                Button("Stop") { chat.stopTelegramBridge() }
                    .disabled(chat.telegramBridgeStatus == "Stopped" || chat.telegramBridgeStatus == "Not configured")
                Text(chat.telegramBridgeStatus)
                    .font(.system(size: 11, design: .monospaced))
                    .foregroundStyle(chat.telegramBridgeStatus.hasPrefix("Running") ? .green : .secondary)
                    .lineLimit(2)
            }

            GroupBox("Setup") {
                VStack(alignment: .leading, spacing: 8) {
                    Text("1. In Telegram, message @BotFather and send /newbot. Paste the token it gives you above and save it.")
                    Text("2. Leave the owner chat ID empty and press Start bridge. It runs in pairing mode and won't act on anything yet.")
                    Text("3. Message your new bot anything in Telegram. It replies with your chat ID — copy that into the field above and save.")
                    Text("4. Press Start bridge again. From then on only messages from that chat run on this Mac; sensitive steps pause and require APPROVE <code> or DENY <code> in Telegram.")
                }
                .font(.system(size: 10))
                .fixedSize(horizontal: false, vertical: true)
                .frame(maxWidth: .infinity, alignment: .leading)
            }
            Text("Log: ~/Library/Logs/Helm/telegram-bridge.log")
                .font(.system(size: 10, design: .monospaced))
                .foregroundStyle(.secondary)
                .textSelection(.enabled)
        }
    }

    private func saveToken() {
        if KeyStore.saveSecret(tokenDraft.trimmingCharacters(in: .whitespacesAndNewlines), key: "telegram.bot-token") {
            saveStatus = "Bot token saved in Keychain."
            tokenDraft = ""
        } else {
            saveStatus = "Could not save the bot token to Keychain."
        }
    }

    private func saveProxy() {
        if KeyStore.saveSecret(proxyDraft.trimmingCharacters(in: .whitespacesAndNewlines), key: "telegram.proxy-url") {
            saveStatus = "Proxy saved in Keychain."
            proxyDraft = ""
        } else {
            saveStatus = "Could not save the proxy to Keychain."
        }
    }

    private func clearProxy() {
        KeyStore.deleteSecret("telegram.proxy-url")
        saveStatus = "Proxy removed."
    }
}

struct DiscordSettingsView: View {
    @EnvironmentObject private var chat: ChatController
    @AppStorage("discord.channel-id") private var channelID = ""
    @AppStorage("discord.owner-user-id") private var ownerUserID = ""
    @AppStorage("discord.auto-start") private var autoStart = false
    @State private var tokenDraft = ""
    @State private var saveStatus: String?

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text("DISCORD BOT")
                .font(.system(size: 11, weight: .semibold, design: .monospaced))
                .tracking(2)
                .foregroundStyle(.secondary)
            Text("Helm polls one Discord channel every few seconds; it can't watch DMs without a persistent Gateway connection, so talk to it in a server channel only you (and the bot) can see. The Mac and Helm must stay awake and online.")
                .font(.system(size: 12))
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)

            HStack(spacing: 10) {
                SecureField(KeyStore.hasSecret("discord.bot-token") ? "Bot token saved (enter to replace)" : "Bot token from the Discord Developer Portal", text: $tokenDraft)
                    .textFieldStyle(.roundedBorder)
                Button("Save") { saveToken() }
                    .disabled(tokenDraft.isEmpty)
            }
            HStack(spacing: 10) {
                TextField("Channel ID", text: $channelID)
                    .textFieldStyle(.roundedBorder)
                TextField("Owner user ID (leave empty to pair)", text: $ownerUserID)
                    .textFieldStyle(.roundedBorder)
                if let saveStatus {
                    Text(saveStatus)
                        .font(.system(size: 11))
                        .foregroundStyle(saveStatus.hasPrefix("Could") ? .orange : .green)
                }
            }

            HStack(spacing: 12) {
                Button {
                    chat.startDiscordBridge()
                } label: {
                    Label("Start bridge", systemImage: "paperplane")
                }
                .disabled(!KeyStore.hasSecret("discord.bot-token") || channelID.isEmpty)
                Button("Stop") { chat.stopDiscordBridge() }
                    .disabled(chat.discordBridgeStatus == "Stopped" || chat.discordBridgeStatus == "Not configured")
                Text(chat.discordBridgeStatus)
                    .font(.system(size: 11, design: .monospaced))
                    .foregroundStyle(chat.discordBridgeStatus.hasPrefix("Running") ? .green : .secondary)
                    .lineLimit(2)
            }
            Toggle("Start automatically when Helm launches", isOn: $autoStart)
                .font(.system(size: 12))

            GroupBox("Setup") {
                VStack(alignment: .leading, spacing: 8) {
                    Text("1. In the Discord Developer Portal, create an Application, add a Bot, and copy its token. Paste it above and save.")
                    Text("2. Invite the bot to your server with the \"Send Messages\" and \"Read Message History\" permissions, and add/use a channel only you can see.")
                    Text("3. In Discord, enable Settings > Advanced > Developer Mode, then right-click the channel for \"Copy Channel ID\". Paste it above.")
                    Text("4. Leave the owner user ID empty and press Start bridge. Message the bot anything in that channel — Helm pairs to whoever sends the first message there, and only that person's messages run anything after that.")
                    Text("5. Sensitive steps pause and require APPROVE <code> or DENY <code> in Discord.")
                }
                .font(.system(size: 10))
                .fixedSize(horizontal: false, vertical: true)
                .frame(maxWidth: .infinity, alignment: .leading)
            }
            Text("Log: ~/Library/Logs/Helm/discord-bridge.log")
                .font(.system(size: 10, design: .monospaced))
                .foregroundStyle(.secondary)
                .textSelection(.enabled)
        }
    }

    private func saveToken() {
        if KeyStore.saveSecret(tokenDraft.trimmingCharacters(in: .whitespacesAndNewlines), key: "discord.bot-token") {
            saveStatus = "Bot token saved in Keychain."
            tokenDraft = ""
        } else {
            saveStatus = "Could not save the bot token to Keychain."
        }
    }
}

struct SlackSettingsView: View {
    @EnvironmentObject private var chat: ChatController
    @AppStorage("slack.channel-id") private var channelID = ""
    @AppStorage("slack.owner-user-id") private var ownerUserID = ""
    @AppStorage("slack.auto-start") private var autoStart = false
    @State private var tokenDraft = ""
    @State private var saveStatus: String?

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text("SLACK BOT")
                .font(.system(size: 11, weight: .semibold, design: .monospaced))
                .tracking(2)
                .foregroundStyle(.secondary)
            Text("Helm polls one Slack channel every few seconds rather than using Socket Mode, so no extra app setup is needed beyond the bot itself. The Mac and Helm must stay awake and online.")
                .font(.system(size: 12))
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)

            HStack(spacing: 10) {
                SecureField(KeyStore.hasSecret("slack.bot-token") ? "Bot token saved (enter to replace)" : "Bot User OAuth Token (xoxb-...)", text: $tokenDraft)
                    .textFieldStyle(.roundedBorder)
                Button("Save") { saveToken() }
                    .disabled(tokenDraft.isEmpty)
            }
            HStack(spacing: 10) {
                TextField("Channel ID (e.g. C0123456789)", text: $channelID)
                    .textFieldStyle(.roundedBorder)
                TextField("Owner user ID (leave empty to pair)", text: $ownerUserID)
                    .textFieldStyle(.roundedBorder)
                if let saveStatus {
                    Text(saveStatus)
                        .font(.system(size: 11))
                        .foregroundStyle(saveStatus.hasPrefix("Could") ? .orange : .green)
                }
            }

            HStack(spacing: 12) {
                Button {
                    chat.startSlackBridge()
                } label: {
                    Label("Start bridge", systemImage: "paperplane")
                }
                .disabled(!KeyStore.hasSecret("slack.bot-token") || channelID.isEmpty)
                Button("Stop") { chat.stopSlackBridge() }
                    .disabled(chat.slackBridgeStatus == "Stopped" || chat.slackBridgeStatus == "Not configured")
                Text(chat.slackBridgeStatus)
                    .font(.system(size: 11, design: .monospaced))
                    .foregroundStyle(chat.slackBridgeStatus.hasPrefix("Running") ? .green : .secondary)
                    .lineLimit(2)
            }
            Toggle("Start automatically when Helm launches", isOn: $autoStart)
                .font(.system(size: 12))

            GroupBox("Setup") {
                VStack(alignment: .leading, spacing: 8) {
                    Text("1. At api.slack.com/apps, create an app, add the chat:write, channels:history, and groups:history (or im:history) scopes under OAuth & Permissions, then install it to your workspace.")
                    Text("2. Copy the Bot User OAuth Token (starts with xoxb-). Paste it above and save.")
                    Text("3. Invite the bot to a channel only you use for this (/invite @YourBotName). Right-click the channel for its ID and paste it above.")
                    Text("4. Leave the owner user ID empty and press Start bridge. Message the bot anything in that channel — Helm pairs to whoever sends the first message there, and only that person's messages run anything after that.")
                    Text("5. Sensitive steps pause and require APPROVE <code> or DENY <code> in Slack.")
                }
                .font(.system(size: 10))
                .fixedSize(horizontal: false, vertical: true)
                .frame(maxWidth: .infinity, alignment: .leading)
            }
            Text("Log: ~/Library/Logs/Helm/slack-bridge.log")
                .font(.system(size: 10, design: .monospaced))
                .foregroundStyle(.secondary)
                .textSelection(.enabled)
        }
    }

    private func saveToken() {
        if KeyStore.saveSecret(tokenDraft.trimmingCharacters(in: .whitespacesAndNewlines), key: "slack.bot-token") {
            saveStatus = "Bot token saved in Keychain."
            tokenDraft = ""
        } else {
            saveStatus = "Could not save the bot token to Keychain."
        }
    }
}

// MARK: - Settings

struct LogsView: View {
    private static let files: [(String, String)] = [
        ("Engine", "backend.log"), ("Telegram", "telegram-bridge.log"), ("WhatsApp", "whatsapp-bridge.log"),
        ("Discord", "discord-bridge.log"), ("Slack", "slack-bridge.log"),
    ]
    @State private var selected = 0
    @State private var contents = "(loading…)"

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("TASK LOGS").font(.system(size: 11, weight: .semibold, design: .monospaced)).tracking(2).foregroundStyle(.secondary)
            Picker("", selection: $selected) {
                ForEach(Self.files.indices, id: \.self) { i in Text(Self.files[i].0).tag(i) }
            }
            .pickerStyle(.segmented)
            .labelsHidden()
            .onChange(of: selected) { _ in reload() }
            ScrollView {
                Text(contents)
                    .font(.system(size: 10, design: .monospaced))
                    .textSelection(.enabled)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
            .frame(height: 420)
            .background(Color.black.opacity(0.2))
            HStack {
                Button("Refresh") { reload() }
                Spacer()
                Text(Self.folder.path).font(.system(size: 9)).foregroundStyle(.secondary).textSelection(.enabled)
            }
        }
        .onAppear { reload() }
    }

    private static var folder: URL {
        FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/Logs/Helm", isDirectory: true)
    }

    private func reload() {
        let url = Self.folder.appendingPathComponent(Self.files[selected].1)
        guard let data = FileManager.default.contents(atPath: url.path), let text = String(data: data, encoding: .utf8) else {
            contents = "No log yet at \(url.path)."
            return
        }
        let lines = text.split(separator: "\n", omittingEmptySubsequences: false)
        contents = lines.suffix(300).joined(separator: "\n")
    }
}

struct SettingsView: View {
    @EnvironmentObject private var chat: ChatController
    @State private var tab = 0

    var body: some View {
        VStack(spacing: 0) {
            Picker("", selection: $tab) {
                Text("Brain").tag(0)
                Text("Abilities").tag(1)
                Text("Permissions").tag(2)
                Text("WhatsApp").tag(3)
                Text("Telegram").tag(4)
                Text("Discord").tag(5)
                Text("Slack").tag(6)
                Text("Logs").tag(7)
            }
            .pickerStyle(.segmented)
            .labelsHidden()
            .padding(20)
            ScrollView {
                Group {
                    switch tab {
                    case 0: ProviderSettingsView()
                    case 1: AbilitiesView()
                    case 2: PermissionsView()
                    case 3: WhatsAppSettingsView()
                    case 4: TelegramSettingsView()
                    case 5: DiscordSettingsView()
                    case 6: SlackSettingsView()
                    default: LogsView()
                    }
                }
                .padding(.horizontal, 24)
                .padding(.bottom, 20)
            }
            HStack {
                Button("Run setup again") {
                    chat.showProviderSettings = false
                    chat.onboarded = false
                }
                Spacer()
                Button("Done") { chat.showProviderSettings = false }
                    .buttonStyle(.borderedProminent)
                    .tint(jarvisCyan)
                    .keyboardShortcut(.defaultAction)
            }
            .padding(20)
        }
        .frame(width: 660, height: 640)
    }
}

// MARK: - Helm's own cursors and the cursor buddy

/// One drawn cursor per agent, separate from the user's mouse (like Codex's agent cursor).
struct CursorState: Identifiable, Equatable {
    let id: String          // "Helm", "Agent 1", ...
    var position: CGPoint
    var action: String
    var visible: Bool
    var clicks: Int
    let color: Color
}

/// Click-through overlay above every window: agent cursors plus a small Helm buddy that rides
/// next to the user's own pointer and says what is going on (HeyClicky-style).
@MainActor
final class AgentCursor: ObservableObject {
    static let shared = AgentCursor()
    static let colors: [String: Color] = [
        "Helm": jarvisCyan,
        "Agent 1": Color(red: 1.0, green: 0.35, blue: 0.75),
        "Agent 2": Color(red: 1.0, green: 0.72, blue: 0.2),
        "Agent 3": Color(red: 0.45, green: 0.95, blue: 0.45),
    ]

    @Published var cursors: [CursorState] = []
    @Published var mouse = CGPoint(x: -200, y: -200)
    @Published var buddyText = ""
    @Published var buddyEnabled = UserDefaults.standard.object(forKey: "cursorBuddy") as? Bool ?? true {
        didSet {
            UserDefaults.standard.set(buddyEnabled, forKey: "cursorBuddy")
            buddyEnabled ? startFollowing() : stopFollowing()
        }
    }
    private var panel: NSPanel?
    private var hideTasks: [String: DispatchWorkItem] = [:]
    private var followTimer: Timer?
    private var screenHeight: CGFloat = 0

    func start() {
        ensurePanel()
        if buddyEnabled { startFollowing() }
    }

    /// `point` uses screen points with a top-left origin (what Accessibility and pyautogui use).
    func move(_ id: String, to point: CGPoint?, action: String, click: Bool) {
        ensurePanel()
        var cursor = cursors.first { $0.id == id }
            ?? CursorState(id: id, position: point ?? CGPoint(x: mouse.x + 60, y: mouse.y + 40), action: "",
                           visible: true, clicks: 0, color: Self.colors[id] ?? jarvisCyan)
        if let point { cursor.position = point }
        cursor.action = action
        cursor.visible = true
        if click { cursor.clicks += 1 }
        if let index = cursors.firstIndex(where: { $0.id == id }) { cursors[index] = cursor } else { cursors.append(cursor) }
        hideTasks[id]?.cancel()
        let task = DispatchWorkItem { [weak self] in
            if let index = self?.cursors.firstIndex(where: { $0.id == id }) { self?.cursors[index].visible = false }
        }
        hideTasks[id] = task
        DispatchQueue.main.asyncAfter(deadline: .now() + 4, execute: task)
    }

    func say(_ text: String) {
        buddyText = String(text.prefix(140))
        hideTasks["buddy"]?.cancel()
        let task = DispatchWorkItem { [weak self] in self?.buddyText = "" }
        hideTasks["buddy"] = task
        DispatchQueue.main.asyncAfter(deadline: .now() + 6, execute: task)
    }

    private func startFollowing() {
        guard followTimer == nil else { return }
        followTimer = Timer.scheduledTimer(withTimeInterval: 1.0 / 30, repeats: true) { [weak self] _ in
            Task { @MainActor in
                guard let self else { return }
                let location = NSEvent.mouseLocation  // bottom-left origin
                self.mouse = CGPoint(x: location.x, y: self.screenHeight - location.y)
            }
        }
    }

    private func stopFollowing() {
        followTimer?.invalidate()
        followTimer = nil
        mouse = CGPoint(x: -200, y: -200)
    }

    private func ensurePanel() {
        guard panel == nil, let screen = NSScreen.screens.first else { return }
        screenHeight = screen.frame.height
        let overlay = NSPanel(contentRect: screen.frame, styleMask: [.borderless, .nonactivatingPanel],
                              backing: .buffered, defer: false)
        overlay.isOpaque = false
        overlay.backgroundColor = .clear
        overlay.hasShadow = false
        overlay.ignoresMouseEvents = true  // never blocks the user's own clicks
        overlay.level = .screenSaver
        overlay.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .stationary, .ignoresCycle]
        overlay.contentView = NSHostingView(rootView: AgentCursorView().environmentObject(self))
        overlay.orderFrontRegardless()
        panel = overlay
    }
}

private struct AgentCursorView: View {
    @EnvironmentObject private var overlay: AgentCursor

    var body: some View {
        ZStack(alignment: .topLeading) {
            Color.clear
            ForEach(overlay.cursors) { cursor in
                OneCursor(cursor: cursor)
            }
            if overlay.buddyEnabled {
                Buddy(text: overlay.buddyText, busy: overlay.cursors.contains { $0.visible })
                    .offset(x: overlay.mouse.x + 16, y: overlay.mouse.y + 18)
            }
        }
        .allowsHitTesting(false)
    }
}

private struct OneCursor: View {
    let cursor: CursorState
    @State private var ripple = false

    var body: some View {
        ZStack(alignment: .topLeading) {
            Circle()
                .stroke(cursor.color, lineWidth: 3)
                .frame(width: ripple ? 54 : 6, height: ripple ? 54 : 6)
                .opacity(ripple ? 0 : 0.9)
                .position(cursor.position)
            HStack(alignment: .top, spacing: 4) {
                CursorArrow()
                    .fill(cursor.color)
                    .overlay(CursorArrow().stroke(Color.white, lineWidth: 1.5))
                    .frame(width: 20, height: 26)
                    .shadow(color: cursor.color.opacity(0.9), radius: 8)
                Text(cursor.action.isEmpty ? cursor.id : "\(cursor.id) · \(cursor.action)")
                    .font(.system(size: 11, weight: .semibold, design: .rounded))
                    .foregroundStyle(.black)
                    .padding(.horizontal, 7)
                    .padding(.vertical, 3)
                    .background(cursor.color.opacity(0.95), in: Capsule())
                    .offset(y: 16)
            }
            .offset(x: cursor.position.x - 2, y: cursor.position.y - 2)
            .animation(.easeInOut(duration: 0.45), value: cursor.position)
        }
        .opacity(cursor.visible ? 1 : 0)
        .animation(.easeOut(duration: 0.3), value: cursor.visible)
        .onChange(of: cursor.clicks) { _ in
            ripple = false
            withAnimation(.easeOut(duration: 0.5)) { ripple = true }
        }
    }
}

/// The little Helm that rides next to your pointer.
private struct Buddy: View {
    let text: String
    let busy: Bool

    var body: some View {
        HStack(alignment: .top, spacing: 6) {
            ZStack {
                Circle().fill(jarvisCyan.opacity(0.35)).blur(radius: 5)
                Circle().fill(AngularGradient(colors: [jarvisCyan, jarvisBlue, jarvisCyan], center: .center))
                Circle().strokeBorder(Color.white.opacity(0.7), lineWidth: 1).padding(3)
            }
            .frame(width: 16, height: 16)
            .scaleEffect(busy ? 1.15 : 1)
            .animation(.easeInOut(duration: 0.5).repeatForever(autoreverses: true), value: busy)
            if !text.isEmpty {
                Text(text)
                    .font(.system(size: 11, weight: .medium, design: .rounded))
                    .foregroundStyle(.white)
                    .lineLimit(3)
                    .frame(maxWidth: 220, alignment: .leading)
                    .padding(.horizontal, 9)
                    .padding(.vertical, 6)
                    .background(Color.black.opacity(0.82), in: RoundedRectangle(cornerRadius: 10))
                    .overlay(RoundedRectangle(cornerRadius: 10).strokeBorder(jarvisCyan.opacity(0.5)))
                    .transition(.opacity)
            }
        }
        .animation(.easeOut(duration: 0.2), value: text)
    }
}

private struct CursorArrow: Shape {
    func path(in rect: CGRect) -> Path {
        var path = Path()
        let w = rect.width, h = rect.height
        path.move(to: CGPoint(x: 0, y: 0))
        path.addLine(to: CGPoint(x: 0, y: h * 0.82))
        path.addLine(to: CGPoint(x: w * 0.28, y: h * 0.62))
        path.addLine(to: CGPoint(x: w * 0.45, y: h))
        path.addLine(to: CGPoint(x: w * 0.62, y: h * 0.92))
        path.addLine(to: CGPoint(x: w * 0.45, y: h * 0.56))
        path.addLine(to: CGPoint(x: w * 0.8, y: h * 0.56))
        path.closeSubpath()
        return path
    }
}


// MARK: - Background agents

/// One background agent: its own Helm engine process, conversation file, cursor colour and approvals.
@MainActor
final class AgentRun: ObservableObject, Identifiable {
    let number: Int
    var id: String { "Agent \(number)" }
    var color: Color { AgentCursor.colors[id] ?? jarvisCyan }
    @Published var task = ""
    @Published var activity: [String] = []
    @Published var reply = ""
    @Published var pendingApproval: String?
    @Published var isRunning = false
    @Published var failed = false
    private weak var owner: ChatController?
    private var process: Process?
    private var inputPipe: Pipe?
    private var buffer = ""

    init(number: Int, owner: ChatController) {
        self.number = number
        self.owner = owner
    }

    private var historyURL: URL {
        FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Application Support/Local Desktop Assistant/agent\(number)_history.json")
    }

    func run(_ text: String) {
        guard let owner, !isRunning else { return }
        try? FileManager.default.removeItem(at: historyURL)  // each task starts fresh
        task = text
        activity = ["Starting…"]
        reply = ""
        failed = false
        let child = Process()
        let output = Pipe()
        let input = Pipe()
        child.executableURL = owner.projectRoot.appendingPathComponent(".venv/bin/python")
        child.arguments = ["-u", owner.projectRoot.appendingPathComponent("assistant.py").path]
            + owner.engineArguments(for: text, history: historyURL)
        child.currentDirectoryURL = owner.projectRoot
        child.standardOutput = output
        child.standardError = output
        child.standardInput = input
        child.environment = owner.engineEnvironment()
        child.terminationHandler = { [weak self] finished in
            DispatchQueue.main.async { self?.finish(status: finished.terminationStatus) }
        }
        output.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            if data.isEmpty { handle.readabilityHandler = nil; return }
            let chunk = String(decoding: data, as: UTF8.self)
            DispatchQueue.main.async { self?.receive(chunk) }
        }
        do {
            try child.run()
            process = child
            inputPipe = input
            isRunning = true
        } catch {
            activity = ["Could not start: \(error.localizedDescription)"]
            failed = true
        }
    }

    func approve(all: Bool = false) { respond(all ? "all\n" : "y\n") }
    func reject() { respond("n\n") }

    func stop() {
        pendingApproval = nil
        process?.terminate()
    }

    private func respond(_ answer: String) {
        guard let inputPipe, pendingApproval != nil else { return }
        inputPipe.fileHandleForWriting.write(Data(answer.utf8))
        pendingApproval = nil
        if answer != "n\n" { NSApp.deactivate() }  // let the agent's target app come forward
    }

    private func receive(_ text: String) {
        buffer += text
        let parts = buffer.components(separatedBy: .newlines)
        buffer = parts.last ?? ""
        for line in parts.dropLast() where !line.trimmingCharacters(in: .whitespaces).isEmpty {
            if line.hasPrefix("APPROVAL_REQUIRED: ") {
                pendingApproval = String(line.dropFirst("APPROVAL_REQUIRED: ".count))
            } else if line.hasPrefix("CURSOR: ") {
                if let data = line.dropFirst("CURSOR: ".count).data(using: .utf8),
                   let info = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
                    let point = (info["x"] as? Double).flatMap { x in (info["y"] as? Double).map { CGPoint(x: x, y: $0) } }
                    AgentCursor.shared.move(id, to: point, action: info["action"] as? String ?? "",
                                            click: info["click"] as? Bool ?? false)
                }
            } else if line.hasPrefix("CHAT_REPLY_JSON:") {
                let json = Data(line.dropFirst("CHAT_REPLY_JSON:".count).utf8)
                reply = (try? JSONDecoder().decode(String.self, from: json)) ?? ""
                AgentCursor.shared.say("\(id): \(reply)")
            } else if line.hasPrefix("TOOL: ") {
                let tool = line.dropFirst("TOOL: ".count).split(separator: " ").first.map(String.init) ?? ""
                note(tool.replacingOccurrences(of: "_", with: " "))
                NSApp.deactivate()
            } else if line.hasPrefix("Error:") {
                note(line)
                failed = true
            } else if !line.hasPrefix("RESULT: ") && !line.hasPrefix("Allow this action?") {
                note(line)
            }
        }
    }

    private func note(_ text: String) {
        activity = Array((activity + [text]).suffix(3))
    }

    private func finish(status: Int32) {
        if status != 0 && reply.isEmpty { failed = true; note("Stopped (exit \(status)).") }
        process = nil
        inputPipe = nil
        pendingApproval = nil
        isRunning = false
    }
}

/// The three agent lanes shown in the HUD.
struct AgentsBoard: View {
    @EnvironmentObject private var chat: ChatController

    var body: some View {
        HStack(spacing: 10) {
            ForEach(chat.agents) { agent in
                AgentLane(agent: agent)
            }
        }
    }
}

private struct AgentLane: View {
    @ObservedObject var agent: AgentRun

    private var status: String {
        if agent.pendingApproval != nil { return "needs your OK" }
        if agent.isRunning { return "working" }
        if agent.failed { return "stopped" }
        return agent.task.isEmpty ? "idle" : "done"
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 5) {
            HStack(spacing: 6) {
                Circle().fill(agent.color).frame(width: 8, height: 8)
                    .shadow(color: agent.color, radius: agent.isRunning ? 4 : 0)
                Text(agent.id.uppercased()).font(.system(size: 10, weight: .bold, design: .monospaced))
                Spacer()
                Text(status).font(.system(size: 9, design: .monospaced)).foregroundStyle(.secondary)
            }
            Text(agent.task.isEmpty ? "Waiting for a task" : agent.task)
                .font(.system(size: 11, weight: .medium))
                .lineLimit(1)
            Text(agent.reply.isEmpty ? agent.activity.joined(separator: " → ") : agent.reply)
                .font(.system(size: 10, design: .monospaced))
                .foregroundStyle(agent.color.opacity(0.85))
                .lineLimit(2)
                .frame(maxWidth: .infinity, minHeight: 26, alignment: .topLeading)
            if let action = agent.pendingApproval {
                Text(action).font(.system(size: 10)).lineLimit(1)
                HStack {
                    Button("Allow") { agent.approve() }
                    Button("All") { agent.approve(all: true) }
                    Button("Stop") { agent.reject() }
                }
                .controlSize(.mini)
            } else if agent.isRunning {
                Button("Stop") { agent.stop() }.controlSize(.mini)
            }
        }
        .padding(9)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(agent.color.opacity(agent.isRunning ? 0.10 : 0.04))
        .overlay(HUDBrackets(color: agent.color.opacity(agent.isRunning ? 0.9 : 0.35)))
    }
}
