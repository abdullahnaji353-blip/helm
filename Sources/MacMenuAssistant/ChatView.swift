import AVFoundation
import AppKit
import Carbon.HIToolbox
import ApplicationServices
import Security
import Speech
import SwiftUI
import UserNotifications

private let jarvisCyan = Color(red: 0.45, green: 0.72, blue: 0.68)
private let jarvisBlue = Color(red: 0.36, green: 0.53, blue: 0.68)
private let helmBackground = Color(red: 0.075, green: 0.078, blue: 0.086)
private let helmSurface = Color(red: 0.115, green: 0.121, blue: 0.132)

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

struct ResearchSource: Codable, Equatable {
    let title: String
    let url: String
    let snippet: String
}

struct WorkflowCheckpoint: Codable, Identifiable {
    let id: Int
    let title: String
    var status: String
    var evidence: String
}

struct AgentWorkflowRecord: Codable {
    let id: String
    let task: String
    var status: String
    var checkpoints: [WorkflowCheckpoint]
    var sources: [ResearchSource]
    var plans: [String: [String]]
    var research: String
    var operation: String
    var verification: String
    var updatedAt: String
}

private func loadDisabledTools() -> Set<String> {
    var disabled = Set(UserDefaults.standard.stringArray(forKey: "disabledTools") ?? [])
    let previouslyGroupedAppTools: Set<String> = ["open_app", "install_app", "uninstall_app"]
    if previouslyGroupedAppTools.isSubset(of: disabled) {
        disabled.formUnion(["list_applications", "list_running_apps", "quit_app"])
    }
    return disabled
}

/// Drives the Helm conversation through `assistant.py --chat`, which keeps
/// history and calls device tools (apps, URLs, volume, status, screen control).
@MainActor
final class ChatController: NSObject, ObservableObject {
    @Published var lines: [ChatLine] = []
    @Published var draft = ""
    @Published private(set) var workflowRecord: AgentWorkflowRecord?
    @Published var isThinking = false
    @Published var isListening = false
    @Published var dictating = false
    private var dictationBase = ""
    @Published var voiceStatus = "Tap ⌃⌥ to open Helm; hold to speak"
    /// Set when a voice message was sent while the HUD window wasn't open, so the reply should
    /// also go to the floating top-right toast, not just the (unseen) chat transcript.
    private var pendingVoiceToast = false
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
    @Published var disabledTools = loadDisabledTools() {
        didSet { UserDefaults.standard.set(Array(disabledTools), forKey: "disabledTools") }
    }
    @Published var browser = UserDefaults.standard.string(forKey: "browser")
        ?? (FileManager.default.fileExists(atPath: "/Applications/Google Chrome.app") ? "chrome" : "safari") {
        didSet { UserDefaults.standard.set(browser, forKey: "browser") }
    }
    @Published var persistentBrowserSession = UserDefaults.standard.bool(forKey: "persistentBrowserSession") {
        didSet { UserDefaults.standard.set(persistentBrowserSession, forKey: "persistentBrowserSession") }
    }
    @Published var voiceHotkey = UserDefaults.standard.object(forKey: "voiceHotkey") as? Bool ?? true {
        didSet {
            UserDefaults.standard.set(voiceHotkey, forKey: "voiceHotkey")
            voice.setShortcutEnabled(voiceHotkey)
        }
    }
    @Published var voiceShortcut = UserDefaults.standard.string(forKey: "voiceShortcut") ?? "control+option" {
        didSet {
            UserDefaults.standard.set(voiceShortcut, forKey: "voiceShortcut")
            voice.setShortcut(voiceShortcut)
        }
    }
    @Published var voiceShortcutTestStatus = ""
    @Published private(set) var voiceShortcutTestPending = false
    @Published private(set) var launchShortcutAvailable = false
    @Published var requestedSettingsTab: Int?
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

    private var workflowRecordURL: URL {
        Self.supportDirectory.appendingPathComponent("agent-workflow.json")
    }

    private var browserProfileDirectory: URL {
        Self.supportDirectory.appendingPathComponent("browser-sessions", isDirectory: true)
    }

    func clearSavedBrowserSessions() throws {
        guard !hasActiveTasks else {
            throw NSError(domain: "Helm", code: 1, userInfo: [NSLocalizedDescriptionKey: "Stop active tasks before clearing browser sessions."])
        }
        if FileManager.default.fileExists(atPath: browserProfileDirectory.path) {
            try FileManager.default.removeItem(at: browserProfileDirectory)
        }
        addActivity("Cleared Helm's saved browser sessions.")
    }

    private func saveWorkflowRecord() {
        guard let workflowRecord else { return }
        do {
            try FileManager.default.createDirectory(
                at: Self.supportDirectory,
                withIntermediateDirectories: true,
                attributes: [.posixPermissions: 0o700]
            )
            let data = try JSONEncoder().encode(workflowRecord)
            try data.write(to: workflowRecordURL, options: .atomic)
            try FileManager.default.setAttributes(
                [.posixPermissions: 0o600],
                ofItemAtPath: workflowRecordURL.path
            )
        } catch {
            lines.append(ChatLine(role: .error, text: "Could not save the task checkpoints: \(error.localizedDescription)"))
        }
    }

    private func restoreWorkflowRecord() {
        guard let data = try? Data(contentsOf: workflowRecordURL),
              var restored = try? JSONDecoder().decode(AgentWorkflowRecord.self, from: data) else { return }
        if restored.status == "running" {
            restored.status = "interrupted"
            for index in restored.checkpoints.indices where restored.checkpoints[index].status == "in_progress" {
                restored.checkpoints[index].status = "interrupted"
            }
            restored.updatedAt = ISO8601DateFormatter().string(from: Date())
            workflowRecord = restored
            saveWorkflowRecord()
        } else {
            workflowRecord = restored
        }
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
    @Published var focusInputAfterOpen = false
    /// Three background agents, each with its own engine, conversation and coloured cursor.
    lazy var agents: [AgentRun] = (1...3).map { AgentRun(number: $0, owner: self) }

    /// Starts `task` on the first idle agent; false if all three are busy.
    @discardableResult
    func dispatch(_ task: String) -> Bool {
        let text = task.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty,
              agents.allSatisfy({ !$0.isRunning && $0.pendingApproval == nil }) else { return false }
        startWorkflow(text)
        objectWillChange.send()
        return true
    }

    private func startWorkflow(_ task: String) {
        workflowRecord = AgentWorkflowRecord(
            id: UUID().uuidString,
            task: task,
            status: "running",
            checkpoints: [
                WorkflowCheckpoint(id: 1, title: "Research and gather evidence", status: "in_progress", evidence: ""),
                WorkflowCheckpoint(id: 2, title: "Perform the task with approvals", status: "pending", evidence: ""),
                WorkflowCheckpoint(id: 3, title: "Verify the outcome", status: "pending", evidence: ""),
            ],
            sources: [],
            plans: [:],
            research: "",
            operation: "",
            verification: "",
            updatedAt: ISO8601DateFormatter().string(from: Date())
        )
        saveWorkflowRecord()
        agents[0].run(task, workflowID: workflowRecord?.id)
    }

    fileprivate func recordPlan(_ plan: [String], from agent: AgentRun) {
        guard var record = workflowRecord, record.id == agent.workflowID else { return }
        record.plans[agent.role] = plan
        record.updatedAt = ISO8601DateFormatter().string(from: Date())
        workflowRecord = record
        saveWorkflowRecord()
    }

    fileprivate func recordSource(_ source: ResearchSource, from agent: AgentRun) {
        guard var record = workflowRecord, record.id == agent.workflowID else { return }
        if !record.sources.contains(where: { $0.url == source.url }) {
            record.sources.append(source)
            record.sources = Array(record.sources.prefix(8))
        }
        record.updatedAt = ISO8601DateFormatter().string(from: Date())
        workflowRecord = record
        saveWorkflowRecord()
    }

    fileprivate func recordAgentActivity(_ text: String, from agent: AgentRun) {
        guard var record = workflowRecord, record.id == agent.workflowID,
              let index = record.checkpoints.firstIndex(where: { $0.id == agent.number }) else { return }
        record.checkpoints[index].evidence = String(text.prefix(500))
        record.updatedAt = ISO8601DateFormatter().string(from: Date())
        workflowRecord = record
        saveWorkflowRecord()
    }

    fileprivate func agentDidFinish(_ agent: AgentRun, status: Int32) {
        guard var record = workflowRecord, record.id == agent.workflowID else { return }
        let successful = status == 0 && !agent.failed && !agent.reply.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
        guard successful else {
            record.status = status == 0 ? "blocked" : "interrupted"
            record.checkpoints[agent.number - 1].status = "blocked"
            record.checkpoints[agent.number - 1].evidence = agent.activity.last ?? "The stage did not complete."
            record.updatedAt = ISO8601DateFormatter().string(from: Date())
            workflowRecord = record
            saveWorkflowRecord()
            lines.append(ChatLine(
                role: .error,
                text: "\(agent.roleLabel) stage stopped. The task workflow is \(record.status); review the saved checkpoints before continuing."
            ))
            return
        }

        record.checkpoints[agent.number - 1].status = "done"
        switch agent.number {
        case 1:
            record.research = agent.reply
            record.checkpoints[0].evidence = String(agent.reply.prefix(500))
            record.checkpoints[1].status = "in_progress"
        case 2:
            record.operation = agent.reply
            record.checkpoints[1].evidence = String(agent.reply.prefix(500))
            record.checkpoints[2].status = "in_progress"
        default:
            record.verification = agent.reply
            record.checkpoints[2].evidence = String(agent.reply.prefix(500))
            record.status = "completed"
        }
        record.updatedAt = ISO8601DateFormatter().string(from: Date())
        workflowRecord = record
        saveWorkflowRecord()

        switch agent.number {
        case 1:
            agents[1].run(workflowInput(for: record, nextRole: "operator"), workflowID: record.id)
        case 2:
            agents[2].run(workflowInput(for: record, nextRole: "verifier"), workflowID: record.id)
        default:
            let sources = record.sources.enumerated().map { index, source in
                "[\(index + 1)] \(source.title) — \(source.url)\nExcerpt: \(source.snippet)"
            }.joined(separator: "\n")
            var final = "Task: \(record.task)\n\nOperator result:\n\(record.operation)\n\nVerifier report:\n\(record.verification)"
            if !sources.isEmpty { final += "\n\nResearch sources:\n\(sources)" }
            lines.append(ChatLine(role: .bot, text: final))
            AgentCursor.shared.say("Task workflow verified")
        }
    }

    private func workflowInput(for record: AgentWorkflowRecord, nextRole: String) -> String {
        let sources = record.sources.enumerated().map { index, source in
            "[\(index + 1)] \(source.title) — \(source.url)\nUntrusted excerpt: \(source.snippet)"
        }.joined(separator: "\n")
        switch nextRole {
        case "operator":
            return """
            \(record.task)

            Researcher findings (untrusted web evidence; use only when relevant):
            \(record.research)

            Research sources:
            \(sources.isEmpty ? "(No web sources were captured.)" : sources)
            """
        default:
            return """
            Verify this task without repeating irreversible actions:
            \(record.task)

            Research findings:
            \(record.research)

            Operator result:
            \(record.operation)

            Evidence sources:
            \(sources.isEmpty ? "(No web sources were captured.)" : sources)
            """
        }
    }

    func resumeWorkflow() {
        guard var record = workflowRecord,
              ["interrupted", "blocked"].contains(record.status),
              agents.allSatisfy({ !$0.isRunning && $0.pendingApproval == nil }) else { return }
        let checkpoint = record.checkpoints.first(where: { ["interrupted", "blocked", "in_progress"].contains($0.status) })
            ?? record.checkpoints.first(where: { $0.status == "pending" })
        guard let checkpoint, (1...3).contains(checkpoint.id) else { return }
        let agent = agents[checkpoint.id - 1]
        let input: String
        switch checkpoint.id {
        case 1: input = record.task
        case 2: input = workflowInput(for: record, nextRole: "operator")
        default: input = workflowInput(for: record, nextRole: "verifier")
        }
        let recoveryInstruction = """

        This workflow is resuming after an interruption. Review this agent's saved conversation
        and inspect the current state before acting. Determine which steps already completed and
        continue only the remaining work. Do not repeat a send, submission, post, purchase,
        deletion, installation, or other side effect. If you cannot verify whether a side effect
        already occurred, stop and ask the user instead of repeating it. Keep all existing
        approval requirements.
        """
        record.status = "running"
        record.checkpoints[checkpoint.id - 1].status = "in_progress"
        record.updatedAt = ISO8601DateFormatter().string(from: Date())
        workflowRecord = record
        saveWorkflowRecord()
        agent.run(input + recoveryInstruction, workflowID: record.id, displayTask: record.task)
    }

    func dispatchDraft() {
        if dispatch(draft) {
            AgentCursor.shared.say("Task sent to a background agent")
            draft = ""
        } else {
            lines.append(ChatLine(role: .error, text: "All three agents are busy. Stop one or wait."))
        }
    }

    func dispatchDraft(to agent: AgentRun) {
        let task = draft.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !task.isEmpty, !agent.isRunning else { return }
        agent.run(task)
        guard agent.isRunning else { return }
        AgentCursor.shared.say("Assigned to \(agent.id)")
        draft = ""
        objectWillChange.send()
    }

    /// The same engine settings the main chat uses.
    func engineArguments(for text: String, history: URL? = nil, agentRole: String? = nil) -> [String] {
        var arguments = ["--chat", text, "--autonomy", askBeforeActing ? "approval" : "autonomous", "--provider", provider]
        if !model(for: provider).isEmpty { arguments += ["--model", model(for: provider)] }
        if !disabledTools.isEmpty { arguments += ["--disable-tools", disabledTools.sorted().joined(separator: ",")] }
        if cloudVision { arguments.append("--cloud-vision") }
        arguments += ["--browser", browser]
        if let history { arguments += ["--history", history.path] }
        if let agentRole {
            arguments += ["--agent-role", agentRole, "--create-plan"]
        }
        return arguments
    }

    func engineEnvironment() -> [String: String] {
        var environment = ProcessInfo.processInfo.environment.merging(ScreenCaptureBridge.shared.environment) { _, new in new }
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
                for line in text.components(separatedBy: .newlines) {
                    if line.hasPrefix("TELEGRAM_BRIDGE_READY:") {
                        self?.telegramBridgeStatus = "Running — " + line.dropFirst("TELEGRAM_BRIDGE_READY:".count)
                    } else if line.hasPrefix("TELEGRAM_BRIDGE_ERROR:") {
                        self?.telegramBridgeStatus = String(line.dropFirst("TELEGRAM_BRIDGE_ERROR:".count))
                    }
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
    private var shortcutTestTimeout: DispatchWorkItem?

    override init() {
        super.init()
        restoreWorkflowRecord()
        installAppShortcut()  // ready as soon as Helm launches
        NotificationCenter.default.addObserver(
            self,
            selector: #selector(applicationDidBecomeActive),
            name: NSApplication.didBecomeActiveNotification,
            object: nil
        )
        configureVoice()  // install the shortcut before a menu or window is opened
        DispatchQueue.main.async { AgentCursor.shared.start() }
        DispatchQueue.main.async { [weak self] in self?.autoStartConfiguredBridges() }
        UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound]) { _, _ in }
    }

    @objc private func applicationDidBecomeActive() {
        guard onboarded, voiceHotkey else { return }
        configureVoice()
        if CGPreflightListenEventAccess() { voice.installHotkey() }
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

    /// helm://settings?tab=... opens a settings page. helm://ask remains off unless enabled,
    /// because any web page could otherwise send Helm commands.
    func handleOpenURL(_ url: URL) {
        if url.host == "settings",
           let components = URLComponents(url: url, resolvingAgainstBaseURL: false),
           let tabName = components.queryItems?.first(where: { $0.name == "tab" })?.value,
           let tab = ["brain": 0, "permissions": 2, "whatsapp": 3, "telegram": 4, "discord": 5, "slack": 6][tabName] {
            requestedSettingsTab = tab
            showProviderSettings = true
            hudRequests += 1
            return
        }
        guard UserDefaults.standard.bool(forKey: "developerURLCommands"),
              let components = URLComponents(url: url, resolvingAgainstBaseURL: false), components.host == "ask",
              let query = components.queryItems?.first(where: { $0.name == "q" })?.value else { return }
        draft = query
        send()
    }

    /// Control+Shift+§ opens Helm from anywhere (§ is the ISO key left of 1; ` on US keyboards).
    func installAppShortcut() {
        guard appShortcuts.isEmpty else { return }
        // Unlike modifier-only shortcuts, this key chord works without Input Monitoring.
        // Helm must already be running in the menu bar to receive any global hotkey.
        let openShortcut = GlobalHotKey(
            keyCode: UInt32(kVK_Space), modifiers: UInt32(controlKey | optionKey), id: 5
        ) { [weak self] in self?.showHUD() }
        appShortcuts.append(openShortcut)
        launchShortcutAvailable = openShortcut.registrationError == nil
        if let error = openShortcut.registrationError {
            lines.append(ChatLine(role: .error, text: "Could not register Control+Option+Space (Carbon status \(error)). Open Helm from its menu bar icon."))
        }
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

    /// Whether the main chat window is currently on screen — not just running in the menu bar.
    var isHUDVisible: Bool {
        NSApp.windows.first(where: { $0.title == "Helm" && !($0 is NSPanel) })?.isVisible == true
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

    func testVoiceShortcut() {
        guard voiceHotkey else {
            voiceShortcutTestStatus = "Turn on the voice shortcut first."
            return
        }
        shortcutTestTimeout?.cancel()
        voiceShortcutTestPending = true
        voiceShortcutTestStatus = "Press and release \(voiceShortcutLabel) within 5 seconds."
        let timeout = DispatchWorkItem { [weak self] in
            guard let self, self.voiceShortcutTestPending else { return }
            self.voiceShortcutTestPending = false
            self.voiceShortcutTestStatus = CGPreflightListenEventAccess()
                ? "No key press detected. Check the selected keys or try again."
                : "Not detected. Enable Input Monitoring, or open Helm with Control+Option+Space."
        }
        shortcutTestTimeout = timeout
        DispatchQueue.main.asyncAfter(deadline: .now() + 5, execute: timeout)
    }

    private var voiceShortcutLabel: String {
        voiceShortcut.split(separator: "+").map { part in
            switch part {
            case "control": "Control"
            case "option": "Option"
            case "shift": "Shift"
            case "command": "Command"
            default: String(part)
            }
        }.joined(separator: "+")
    }

    func voiceShortcutWasPressed() {
        guard voiceShortcutTestPending else { showHUD(); return }
        shortcutTestTimeout?.cancel()
        voiceShortcutTestPending = false
        voiceShortcutTestStatus = "Shortcut detected. Tap opens Helm; holding starts voice input."
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
        voice.onShortcutTap = { [weak self] in self?.voiceShortcutWasPressed() }
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
                if !self.isHUDVisible {
                    self.pendingVoiceToast = true
                    VoiceToast.shared.show(you: transcript)
                }
                self.send()
            }
        }
        voice.setShortcut(voiceShortcut)
        voice.setShortcutEnabled(voiceHotkey)
    }

    func toggleVoice() {
        configureVoice()
        voice.toggleCapture()
    }

    func retryVoiceShortcut() {
        configureVoice()
        voice.installHotkey()
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
    fileprivate static let actingTools: Set<String> = [
        "open_app", "open_url", "send_message", "read_chat", "control_screen", "resume_task", "click_in_app", "menu_in_app",
        "type_in_app", "keys_in_app", "new_document", "paste_into_app", "web_search", "ask_chatgpt", "media_control",
    ]

    /// macOS won't let a background process pull focus away from the app the user is typing in,
    /// and that app is Helm itself. Give up focus (the window stays visible) so the target can come forward.
    private func stepAside() {
        NSApp.deactivate()
    }

    func takeOver(from agent: AgentRun) {
        if agent.isRunning { agent.stop() }
        let progress = agent.activity.suffix(8).map { "- \($0)" }.joined(separator: "\n")
        draft = "Take over this task from \(agent.id): \(agent.task)\n\nProgress so far:\n\(progress.isEmpty ? "- No activity recorded yet." : progress)\n\nContinue from the current state. Do not repeat completed or irreversible actions; verify what is already done first."
        showHUD()
    }

    private func logBackend(_ text: String) {
        if let error = Self.appendToHelmLog(filename: "backend.log", text: text) {
            lines.append(ChatLine(role: .error, text: "Could not securely write the backend log: \(error)"))
        }
    }

    /// Appends redacted text to ~/Library/Logs/Helm/<filename>, creating the folder and file with
    /// owner-only permissions if needed. Shared by the main chat log and each agent's own log.
    fileprivate static func appendToHelmLog(filename: String, text: String) -> String? {
        let folder = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/Logs/Helm", isDirectory: true)
        let url = folder.appendingPathComponent(filename)
        let redacted = redactLogSecrets(text)
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
            return nil
        } catch {
            return error.localizedDescription
        }
    }

    fileprivate static func redactLogSecrets(_ text: String) -> String {
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
        var environment = ProcessInfo.processInfo.environment.merging(ScreenCaptureBridge.shared.environment) { _, new in new }
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
                if pendingVoiceToast { VoiceToast.shared.answer("Needs your OK — open Helm to approve.") }
            } else if line.hasPrefix("CHAT_REPLY_JSON:") {
                let json = Data(line.dropFirst("CHAT_REPLY_JSON:".count).utf8)
                if let reply = try? JSONDecoder().decode(String.self, from: json) {
                    lines.append(ChatLine(role: .bot, text: reply))
                    AgentCursor.shared.say(reply)
                    speak(reply)
                    if pendingVoiceToast {
                        pendingVoiceToast = false
                        VoiceToast.shared.answer(reply)
                    }
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

    fileprivate static let toolLabels = [
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
        if pendingVoiceToast {
            // No CHAT_REPLY_JSON ever arrived (error/approval-declined/stop) — say so instead of
            // leaving the toast stuck on "Thinking…".
            pendingVoiceToast = false
            VoiceToast.shared.answer(status == 0 ? "Done." : "That didn't finish — check Helm.")
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
        Group {
            if chat.isListening {
                Image(systemName: "waveform")
            } else {
                HelmBrandMark(size: 18)
            }
        }
            .onChange(of: chat.hudRequests) { _ in
                openWindow(id: "hud")
                NSApp.activate(ignoringOtherApps: true)
            }
            .onOpenURL { chat.handleOpenURL($0) }
    }
}

private struct HelmBrandMark: View {
    var size: CGFloat = 32

    var body: some View {
        Image(nsImage: NSWorkspace.shared.icon(forFile: Bundle.main.bundleURL.path))
            .resizable()
            .interpolation(.high)
            .frame(width: size, height: size)
            .clipShape(RoundedRectangle(cornerRadius: size * 0.23, style: .continuous))
            .accessibilityHidden(true)
    }
}

private struct ShortcutKey: View {
    let title: String

    var body: some View {
        Text(title)
            .font(.system(size: 9, weight: .medium, design: .rounded))
            .foregroundStyle(.primary.opacity(0.86))
            .padding(.horizontal, 7)
            .padding(.vertical, 5)
            .background(Color.white.opacity(0.07), in: RoundedRectangle(cornerRadius: 5))
            .overlay(RoundedRectangle(cornerRadius: 5).strokeBorder(Color.white.opacity(0.09)))
    }
}

/// In HUD mode the menu bar icon is just a launcher, so there is only one chat UI.
struct MenuLauncher: View {
    @EnvironmentObject private var chat: ChatController
    @Environment(\.openWindow) private var openWindow

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack(spacing: 11) {
                HelmBrandMark(size: 42)
                    .shadow(color: jarvisCyan.opacity(0.18), radius: 12, y: 3)
                VStack(alignment: .leading, spacing: 4) {
                    Text("H E L M")
                        .font(.system(size: 12, weight: .bold, design: .rounded))
                        .tracking(1.5)
                    Text("Your Mac copilot")
                        .font(.system(size: 10))
                        .foregroundStyle(.secondary)
                }
                Spacer(minLength: 4)
                HStack(spacing: 5) {
                    Circle().fill(chat.isThinking ? jarvisCyan : Color.green).frame(width: 6, height: 6)
                    Text(!chat.onboarded ? "SETUP" : chat.isThinking ? "ACTIVE" : "READY")
                        .font(.system(size: 8, weight: .bold, design: .monospaced))
                        .tracking(0.5)
                        .foregroundStyle(.secondary)
                }
            }

            Button(action: openHelm) {
                HStack(spacing: 10) {
                    Image(systemName: chat.onboarded ? "arrow.up.forward.app" : "sparkles")
                        .font(.system(size: 14, weight: .semibold))
                    Text(chat.onboarded ? "Open Helm" : "Finish setup")
                        .font(.system(size: 12, weight: .semibold))
                    Spacer()
                    Image(systemName: "arrow.up.right")
                        .font(.system(size: 10, weight: .semibold))
                        .opacity(0.78)
                }
                .foregroundStyle(Color.white)
                .padding(.horizontal, 14)
                .frame(maxWidth: .infinity, minHeight: 42)
                .background(
                    LinearGradient(colors: [jarvisBlue.opacity(0.82), jarvisCyan.opacity(0.62)], startPoint: .topLeading, endPoint: .bottomTrailing),
                    in: RoundedRectangle(cornerRadius: 11, style: .continuous)
                )
                .overlay(RoundedRectangle(cornerRadius: 11, style: .continuous).strokeBorder(Color.white.opacity(0.13)))
            }
            .buttonStyle(.plain)
            .accessibilityLabel(chat.onboarded ? "Open Helm" : "Finish Helm setup")

            HStack(spacing: 7) {
                Image(systemName: "keyboard")
                    .font(.system(size: 10, weight: .medium))
                    .foregroundStyle(.secondary)
                Text(chat.launchShortcutAvailable ? "Open from anywhere" : "Keyboard shortcut unavailable")
                    .font(.system(size: 9))
                    .foregroundStyle(.secondary)
                Spacer(minLength: 4)
                if chat.launchShortcutAvailable {
                    ShortcutKey(title: "⌃")
                    ShortcutKey(title: "⌥")
                    ShortcutKey(title: "Space")
                }
            }
            .padding(9)
            .background(Color.white.opacity(0.035), in: RoundedRectangle(cornerRadius: 9))
            .overlay(RoundedRectangle(cornerRadius: 9).strokeBorder(Color.white.opacity(0.055)))

            Button {
                openHelm()
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.15) {
                    chat.focusInputAfterOpen = true
                }
            } label: {
                Label("Quick Ask", systemImage: "bubble.left.and.bubble.right")
                    .font(.system(size: 10, weight: .medium))
                    .frame(maxWidth: .infinity, minHeight: 30)
                    .background(Color.white.opacity(0.045), in: RoundedRectangle(cornerRadius: 8))
            }
            .buttonStyle(.plain)

            if chat.onboarded && chat.voiceHotkey && !CGPreflightListenEventAccess() {
                Button {
                    PermissionManager.request("input-monitoring")
                } label: {
                    Label("Set up voice shortcut", systemImage: "waveform.badge.exclamationmark")
                        .font(.system(size: 10, weight: .medium))
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
                .buttonStyle(.plain)
                .foregroundStyle(.orange)
                .padding(.horizontal, 2)
            }

            Rectangle().fill(Color.white.opacity(0.07)).frame(height: 1)
            HStack(spacing: 8) {
                Button {
                    chat.showProviderSettings = true
                    openHelm()
                } label: {
                    Label("Settings", systemImage: "gearshape")
                        .font(.system(size: 10, weight: .medium))
                        .frame(maxWidth: .infinity, minHeight: 30)
                        .background(Color.white.opacity(0.045), in: RoundedRectangle(cornerRadius: 8))
                }
                .buttonStyle(.plain)

                Button { NSApplication.shared.terminate(nil) } label: {
                    Image(systemName: "power")
                        .font(.system(size: 12, weight: .medium))
                        .frame(width: 36, height: 30)
                        .background(Color.white.opacity(0.045), in: RoundedRectangle(cornerRadius: 8))
                }
                .buttonStyle(.plain)
                .help("Quit Helm")
            }
        }
        .padding(15)
        .frame(width: 310)
        .background(helmBackground)
        .preferredColorScheme(.dark)
        .onAppear { chat.configureVoice() }
    }

    private func openHelm() {
        openWindow(id: "hud")
        NSApp.activate(ignoringOtherApps: true)
    }
}

/// Chat by default; the original panel stays reachable for permissions and MCP.
struct ChatRootView: View {
    @EnvironmentObject private var chat: ChatController
    @State private var showAdvanced = false

    var body: some View {
        if chat.interfaceStyle == "hud" || !chat.onboarded {
            MenuLauncher()
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
            Rectangle().fill(Color.white.opacity(0.08)).frame(height: 1)
            transcript
            if let action = chat.pendingApproval {
                approvalCard(action)
            }
            inputBar
        }
        .background(helmBackground)
        .preferredColorScheme(.dark)
        .onAppear { chat.configureVoice() }
    }

    private var status: String {
        if chat.isListening { return "Listening…" }
        if chat.pendingApproval != nil { return "Waiting for your OK" }
        if chat.isThinking { return "Thinking…" }
        if chat.voiceHotkey && !CGPreflightListenEventAccess() { return "⌃⌥Space opens Helm · voice shortcut needs Input Monitoring" }
        return chat.voiceHotkey ? "⌃⌥Space opens Helm · tap Control+Option or hold to speak" : "⌃⌥Space opens Helm · voice shortcut off"
    }

    private var header: some View {
        HStack(spacing: 12) {
            HelmBrandMark(size: 29)
            VStack(alignment: .leading, spacing: 2) {
                Text("Helm").font(.system(size: 12, weight: .semibold))
                Text(status)
                    .font(.system(size: 10))
                    .foregroundStyle(.secondary)
            }
            Spacer()
            if chat.voiceHotkey && !CGPreflightListenEventAccess() {
                Button("Enable shortcut") { PermissionManager.request("input-monitoring") }
                    .font(.system(size: 9, weight: .medium)).buttonStyle(.bordered).controlSize(.mini)
            }
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
                Image(systemName: "ellipsis")
                    .font(.system(size: 13, weight: .medium))
                    .frame(width: 26, height: 26)
                    .background(Color.white.opacity(0.05), in: Circle())
                    .foregroundStyle(.secondary)
            }
            .menuStyle(.borderlessButton)
            .menuIndicator(.hidden)
            .fixedSize()
            .accessibilityLabel("Settings")
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 11)
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
                .padding(.horizontal, 15)
                .padding(.vertical, 14)
            }
            .onChange(of: chat.lines) { _ in
                withAnimation(.easeOut(duration: 0.2)) {
                    proxy.scrollTo(chat.isThinking ? AnyHashable("typing") : AnyHashable(chat.lines.last?.id), anchor: .bottom)
                }
            }
        }
    }

    private var welcome: some View {
        VStack(alignment: .leading, spacing: 9) {
            Text("What can I help with?")
                .font(.system(size: 21, weight: .semibold))
                .padding(.top, 20)
            Text("Ask a question or describe a task for your Mac.")
                .font(.system(size: 11)).foregroundStyle(.secondary)
            VStack(spacing: 5) {
                ForEach(suggestions, id: \.self) { suggestion in
                    Button {
                        chat.draft = suggestion
                        chat.send()
                    } label: {
                        HStack(spacing: 8) {
                            Image(systemName: "arrow.turn.down.right").font(.system(size: 9)).foregroundStyle(.tertiary)
                            Text(suggestion).font(.system(size: 11))
                            Spacer(minLength: 0)
                        }
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .padding(.horizontal, 9).padding(.vertical, 8)
                        .background(Color.white.opacity(0.035), in: RoundedRectangle(cornerRadius: 7))
                    }
                    .buttonStyle(.plain).foregroundStyle(.primary.opacity(0.82))
                }
            }
            .padding(.top, 4)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    @ViewBuilder
    private func bubble(_ line: ChatLine) -> some View {
        switch line.role {
        case .user:
            HStack {
                Spacer(minLength: 50)
                Text(line.text)
                    .font(.system(size: 12))
                    .textSelection(.enabled)
                    .padding(.horizontal, 12).padding(.vertical, 8)
                    .background(Color.white.opacity(0.09), in: RoundedRectangle(cornerRadius: 11))
            }
        case .bot:
            HStack(alignment: .top, spacing: 8) {
                HelmBrandMark(size: 21)
                Text(line.text)
                    .font(.system(size: 12))
                    .textSelection(.enabled)
                    .padding(.top, 2)
                Spacer(minLength: 50)
            }
        case .activity:
            Label {
                Text(line.text).textSelection(.enabled)
            } icon: {
                Image(systemName: "gearshape.2").foregroundStyle(jarvisCyan)
            }
            .font(.system(size: 10))
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
        .background(Color.orange.opacity(0.08), in: RoundedRectangle(cornerRadius: 10))
        .padding(.horizontal, 14)
        .padding(.bottom, 8)
    }

    private var inputBar: some View {
        HStack(spacing: 8) {
            Button {
                chat.toggleDictation()
            } label: {
                Image(systemName: chat.isListening && chat.dictating ? "waveform" : "mic.fill")
                    .font(.system(size: 13, weight: .semibold))
                    .foregroundStyle(chat.isListening && chat.dictating ? .white : jarvisCyan)
                    .frame(width: 29, height: 29)
                    .background(chat.isListening && chat.dictating ? Color.red : Color.white.opacity(0.06), in: RoundedRectangle(cornerRadius: 8))
            }
            .buttonStyle(.plain)
            .help("Dictate into the message box; review before sending")
            .accessibilityLabel(chat.isListening && chat.dictating ? "Stop dictation" : "Dictate")

            TextField("Message Helm…", text: $chat.draft, axis: .vertical)
                .textFieldStyle(.plain)
                .font(.system(size: 13))
                .lineLimit(1...4)
                .onSubmit { chat.send() }
                .disabled(chat.isThinking)

            if chat.isThinking {
                Button { chat.stop() } label: {
                    Image(systemName: "stop.fill").font(.system(size: 11, weight: .semibold))
                        .frame(width: 29, height: 29).background(Color.red.opacity(0.8), in: RoundedRectangle(cornerRadius: 8))
                }
                .buttonStyle(.plain)
                .foregroundStyle(.red.opacity(0.85))
                .help("Stop")
            } else {
                Button { chat.send() } label: {
                    Image(systemName: "arrow.up").font(.system(size: 12, weight: .semibold))
                        .frame(width: 29, height: 29).background(jarvisCyan, in: RoundedRectangle(cornerRadius: 8))
                }
                .buttonStyle(.plain)
                .foregroundStyle(.white)
                .disabled(chat.draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                .help("Send")
            }
        }
        .padding(.horizontal, 9).padding(.vertical, 7)
        .background(Color.white.opacity(0.045), in: RoundedRectangle(cornerRadius: 11))
        .overlay(RoundedRectangle(cornerRadius: 11).strokeBorder(Color.white.opacity(0.09)))
        .padding(12)
    }
}

private struct Orb: View {
    let active: Bool
    let size: CGFloat

    var body: some View {
        ZStack {
            Circle().fill(Color.white.opacity(0.035))
            Circle().strokeBorder(Color.white.opacity(0.12), lineWidth: 1)
            Circle().fill(jarvisCyan.opacity(active ? 0.95 : 0.58))
                .frame(width: size * 0.23, height: size * 0.23)
            if active {
                Circle().strokeBorder(jarvisCyan.opacity(0.45), lineWidth: 1)
                    .padding(size * 0.20)
            }
        }
        .frame(width: size, height: size)
        .accessibilityHidden(true)
    }
}

private struct TypingDots: View {
    var body: some View {
        HStack(spacing: 7) {
            ProgressView().controlSize(.small).tint(jarvisCyan)
            Text("Helm is working…").font(.system(size: 10)).foregroundStyle(.secondary)
        }
        .padding(.leading, 33).padding(.vertical, 5)
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
            helmBackground.ignoresSafeArea()
            if !chat.onboarded { OnboardingView() }
            else if showAdvanced { ChatRootViewAdvanced(showAdvanced: $showAdvanced) }
            else {
                HStack(spacing: 0) {
                    workspaceSidebar.frame(width: 238)
                    Rectangle().fill(Color.white.opacity(0.07)).frame(width: 1)
                    conversationColumn
                }
            }
        }
        .preferredColorScheme(.dark)
        .frame(minWidth: 1020, minHeight: 680)
        .sheet(isPresented: $chat.showProviderSettings) { SettingsView().preferredColorScheme(.dark) }
        .onOpenURL { chat.handleOpenURL($0) }
        .onAppear {
            chat.configureVoice()
            if chat.onboarded && chat.interfaceStyle == "menubar" && chat.hudRequests == 0 { closeHUDWindow() }
        }
        .onChange(of: chat.interfaceStyle) { style in
            if style == "menubar" && chat.onboarded && !chat.showProviderSettings { closeHUDWindow() }
        }
    }

    private func closeHUDWindow() {
        DispatchQueue.main.async { NSApp.windows.filter { $0.title == "Helm" && !($0 is NSPanel) }.forEach { $0.close() } }
    }

    private func openSettings(_ tab: Int) {
        chat.requestedSettingsTab = tab
        chat.showProviderSettings = true
    }

    private var workspaceSidebar: some View {
        VStack(alignment: .leading, spacing: 17) {
            HStack(spacing: 9) {
                HelmBrandMark(size: 28)
                Text("Helm").font(.system(size: 14, weight: .semibold))
                Spacer()
            }
            Button { chat.newChat() } label: {
                Label("New task", systemImage: "plus")
                    .font(.system(size: 12, weight: .medium))
                    .frame(maxWidth: .infinity, alignment: .leading).padding(.horizontal, 10).padding(.vertical, 9)
                    .background(Color.white.opacity(0.055), in: RoundedRectangle(cornerRadius: 8))
                    .overlay(RoundedRectangle(cornerRadius: 8).strokeBorder(Color.white.opacity(0.06)))
            }.buttonStyle(.plain).foregroundStyle(.primary)

            VStack(alignment: .leading, spacing: 3) {
                sidebarSection("Workspace")
                sidebarButton("Chat", "bubble.left.and.bubble.right", selected: true) {}
                sidebarButton("Integrations", "square.grid.2x2") { openSettings(3) }
                sidebarButton("Permissions", "hand.raised") { openSettings(2) }
                sidebarButton("Activity", "clock.arrow.circlepath") { openSettings(7) }
            }
            VStack(alignment: .leading, spacing: 2) {
                sidebarSection("Connections")
                connectorButton("Telegram", "paperplane.fill", 4)
                connectorButton("Slack", "number", 6)
                connectorButton("WhatsApp", "message.fill", 3)
                connectorButton("Discord", "bubble.left.and.bubble.right.fill", 5)
            }
            Spacer(minLength: 10)
            Button { openSettings(0) } label: {
                HStack(spacing: 9) {
                    Image(systemName: "cpu").font(.system(size: 13)).foregroundStyle(.secondary)
                    VStack(alignment: .leading, spacing: 2) {
                        Text("AI model").font(.system(size: 10)).foregroundStyle(.secondary)
                        Text(chat.provider == "ollama" ? "On this Mac" : ProviderOption.find(chat.provider).label)
                            .font(.system(size: 11, weight: .medium)).lineLimit(1)
                    }
                    Spacer(minLength: 0)
                    Image(systemName: "chevron.right").font(.system(size: 9, weight: .semibold)).foregroundStyle(.tertiary)
                }
                .padding(.horizontal, 9).padding(.vertical, 9)
                .background(Color.white.opacity(0.04), in: RoundedRectangle(cornerRadius: 8))
            }.buttonStyle(.plain)
        }
        .padding(14).padding(.vertical, 8).frame(maxHeight: .infinity)
        .background(Color.white.opacity(0.018))
    }

    private func sidebarSection(_ text: String) -> some View {
        Text(text).font(.system(size: 10, weight: .medium)).foregroundStyle(.secondary)
            .padding(.horizontal, 10).padding(.top, 6).padding(.bottom, 3)
    }

    private func sidebarButton(_ title: String, _ icon: String, selected: Bool = false, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Label(title, systemImage: icon).font(.system(size: 11, weight: selected ? .medium : .regular))
                .foregroundStyle(selected ? Color.primary : Color.secondary)
                .frame(maxWidth: .infinity, alignment: .leading).padding(.horizontal, 10).padding(.vertical, 7)
                .background(selected ? Color.white.opacity(0.075) : .clear, in: RoundedRectangle(cornerRadius: 7))
        }.buttonStyle(.plain)
    }

    private func connectorButton(_ title: String, _ icon: String, _ tab: Int) -> some View {
        let bridge: String? = switch tab {
        case 3: chat.whatsappBridgeStatus
        case 4: chat.telegramBridgeStatus
        case 5: chat.discordBridgeStatus
        case 6: chat.slackBridgeStatus
        default: nil
        }
        let connected = bridge?.hasPrefix("Running") == true
        let connectionLabel: String = connected ? "Connected" :
            (bridge == "Not configured" || bridge == "Stopped" ? "Setup" : "Check")
        return Button { openSettings(tab) } label: {
            HStack(spacing: 8) {
                Image(systemName: icon).font(.system(size: 10)).foregroundStyle(.secondary).frame(width: 15)
                Text(title).font(.system(size: 10)).foregroundStyle(.primary.opacity(0.82))
                Spacer()
                Text(connectionLabel).font(.system(size: 8, weight: .medium))
                    .foregroundStyle(connected ? Color.green : Color.secondary)
                Circle().fill(connected ? Color.green : Color.white.opacity(0.20)).frame(width: 5, height: 5)
            }.padding(.horizontal, 10).padding(.vertical, 6).contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .help(bridge ?? "Open integration settings")
        .accessibilityLabel("\(title), \(connectionLabel). Open integration settings.")
    }

    private var conversationColumn: some View {
        VStack(spacing: 0) {
            HStack(spacing: 12) {
                VStack(alignment: .leading, spacing: 3) {
                    HStack(spacing: 7) {
                        Text("Chat").font(.system(size: 14, weight: .semibold))
                        if chat.isThinking || chat.isListening {
                            Circle().fill(chat.isListening ? Color.orange : jarvisCyan).frame(width: 6, height: 6)
                        }
                    }
                    Text(chat.isListening ? "Listening" : chat.pendingApproval != nil ? "Waiting for approval" : chat.isThinking ? "Working on your request" : "")
                        .font(.system(size: 10)).foregroundStyle(.secondary).frame(height: 12, alignment: .leading)
                }
                Spacer()
                if chat.voiceHotkey && !CGPreflightListenEventAccess() {
                    Button("Enable shortcut") { PermissionManager.request("input-monitoring") }
                        .font(.system(size: 9, weight: .medium)).buttonStyle(.bordered).controlSize(.mini)
                }
                Menu {
                    Toggle("Ask before actions", isOn: $chat.askBeforeActing)
                    Toggle("Speak replies", isOn: $chat.speakReplies)
                    if chat.hasActiveTasks { Button("Stop all tasks", role: .destructive) { chat.emergencyStop() } }
                    Divider()
                    Button("New chat") { chat.newChat() }
                    Button("AI model & API keys…") { openSettings(0) }
                    Button("Open Permissions…") { openSettings(2) }
                    Button("Abilities…") { openSettings(1) }
                    Button("MCP tools…") { showAdvanced = true }
                    Divider()
                    Button("Quit Helm") { NSApplication.shared.terminate(nil) }
                } label: {
                    Image(systemName: "ellipsis").font(.system(size: 14, weight: .semibold)).foregroundStyle(.primary.opacity(0.8))
                        .frame(width: 34, height: 34).background(helmSurface, in: Circle())
                }.menuStyle(.borderlessButton).menuIndicator(.hidden).fixedSize()
            }
            .padding(.horizontal, 26).padding(.vertical, 12).background(helmBackground)
            Rectangle().fill(Color.white.opacity(0.06)).frame(height: 1)
            ScrollViewReader { proxy in
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 20) {
                        if chat.lines.isEmpty { hudWelcome }
                        ForEach(chat.lines) { line in hudLine(line).id(line.id) }
                        if chat.isThinking && chat.pendingApproval == nil { TypingDots().id("typing") }
                    }.frame(maxWidth: 760).frame(maxWidth: .infinity)
                        .padding(.horizontal, 30).padding(.vertical, 22)
                }
                .onChange(of: chat.lines) { _ in
                    withAnimation(.easeOut(duration: 0.2)) { proxy.scrollTo(chat.isThinking ? AnyHashable("typing") : AnyHashable(chat.lines.last?.id), anchor: .bottom) }
                }
            }
            if let action = chat.pendingApproval { HUDApproval(action: action) }
            AgentsBoard().padding(.horizontal, 24)
            HUDInput()
        }
    }

    private var greeting: String {
        switch Calendar.current.component(.hour, from: Date()) {
        case 5..<12: return "Good morning"
        case 12..<18: return "Good afternoon"
        default: return "Good evening"
        }
    }

    private var hudWelcome: some View {
        VStack(spacing: 10) {
            HelmBrandMark(size: 76)
                .shadow(color: jarvisBlue.opacity(0.18), radius: 24, y: 8)
                .padding(.bottom, 2)
            Text("H E L M")
                .font(.system(size: 20, weight: .semibold, design: .rounded))
                .tracking(3)
            Text("Think. Create. Solve. Together.")
                .font(.system(size: 12))
                .foregroundStyle(.secondary)
            Text(greeting + ". What are we working on?")
                .font(.system(size: 14, weight: .medium))
                .padding(.top, 7)
            LazyVGrid(columns: [GridItem(.flexible()), GridItem(.flexible()), GridItem(.flexible())], alignment: .center, spacing: 8) {
                ForEach(Array([
                    ("Search the web", "magnifyingglass", "Search the web for "),
                    ("Analyze a file", "doc.text", "Analyze the file I attach and summarize what matters."),
                    ("Code & debug", "chevron.left.forwardslash.chevron.right", "Help me debug this code. Explain the issue, then suggest a fix."),
                    ("Guide me in an app", "cursorarrow.motionlines", "Look at my current screen and guide me through what I'm trying to do, one clear step at a time."),
                    ("Draft an email", "envelope", "Help me draft an email. Ask about the recipient, purpose, and tone, then show me the draft before anything is sent."),
                    ("Ask about this screen", "viewfinder", "Look at my current screen and explain what I'm seeing and one useful next step.")
                ].enumerated()), id: \.offset) { entry in
                    let suggestion = entry.element
                    Button { chat.draft = suggestion.2 } label: {
                        VStack(spacing: 7) {
                            Image(systemName: suggestion.1)
                                .font(.system(size: 14, weight: .medium))
                                .foregroundStyle(jarvisCyan)
                            Text(suggestion.0)
                                .font(.system(size: 9, weight: .medium))
                                .lineLimit(1)
                        }
                        .frame(maxWidth: .infinity, minHeight: 58)
                        .background(Color.white.opacity(0.035), in: RoundedRectangle(cornerRadius: 10))
                        .overlay(RoundedRectangle(cornerRadius: 10).strokeBorder(Color.white.opacity(0.075)))
                    }
                    .buttonStyle(.plain)
                    .foregroundStyle(.primary.opacity(0.82))
                }
            }
            .padding(.top, 4)
        }
        .frame(maxWidth: .infinity)
        .padding(.top, 10)
    }

    @ViewBuilder
    private func hudLine(_ line: ChatLine) -> some View {
        switch line.role {
        case .user:
            HStack {
                Spacer(minLength: 70)
                Text(line.text).font(.system(size: 12)).lineSpacing(3).textSelection(.enabled)
                    .padding(.horizontal, 13).padding(.vertical, 9)
                    .background(Color.white.opacity(0.09), in: RoundedRectangle(cornerRadius: 12))
            }
        case .bot:
            HStack(alignment: .top, spacing: 10) {
                HelmBrandMark(size: 23)
                Text(line.text).font(.system(size: 12)).lineSpacing(4).textSelection(.enabled).padding(.top, 2)
                Spacer(minLength: 18)
            }
        case .activity:
            Label(line.text, systemImage: "checkmark.circle").font(.system(size: 10)).foregroundStyle(.secondary).textSelection(.enabled).padding(.leading, 33)
        case .error:
            Label(line.text, systemImage: "exclamationmark.triangle.fill").font(.system(size: 11)).foregroundStyle(.orange).textSelection(.enabled)
                .padding(10).frame(maxWidth: .infinity, alignment: .leading).background(Color.orange.opacity(0.07), in: RoundedRectangle(cornerRadius: 9))
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
    @FocusState private var inputFocused: Bool

    var body: some View {
        HStack(spacing: 11) {
            Button { chat.toggleDictation() } label: {
                Image(systemName: chat.isListening && chat.dictating ? "waveform" : "mic.fill")
                    .font(.system(size: 12, weight: .medium))
                    .foregroundStyle(chat.isListening && chat.dictating ? .white : jarvisCyan)
                    .frame(width: 30, height: 30)
                    .background(chat.isListening && chat.dictating ? Color.red.opacity(0.8) : Color.white.opacity(0.06), in: RoundedRectangle(cornerRadius: 8))
            }
            .buttonStyle(.plain)
            .help("Dictate into the message box")
            .accessibilityLabel(chat.isListening && chat.dictating ? "Stop dictation" : "Dictate")

            TextField("Message Helm…", text: $chat.draft, axis: .vertical)
                .textFieldStyle(.plain)
                .font(.system(size: 12))
                .lineLimit(1...4)
                .focused($inputFocused)
                .onSubmit { chat.send() }
                .disabled(chat.isThinking)
                .padding(.vertical, 5)

            if chat.isThinking {
                Button { chat.stop() } label: {
                    Image(systemName: "stop.fill").font(.system(size: 10, weight: .bold))
                        .foregroundStyle(.white).frame(width: 30, height: 30).background(Color.red.opacity(0.82), in: RoundedRectangle(cornerRadius: 8))
                }
                .buttonStyle(.plain)
                .help("Stop Helm")
            } else {
                Button { chat.dispatchDraft() } label: {
                    Image(systemName: "arrowshape.turn.up.right").font(.system(size: 12, weight: .medium))
                        .foregroundStyle(.secondary).frame(width: 30, height: 30)
                }
                .buttonStyle(.plain)
                .disabled(chat.draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                .help("Run this request in the background")

                Button { chat.send() } label: {
                    Image(systemName: "arrow.up").font(.system(size: 12, weight: .semibold))
                        .foregroundStyle(.white).frame(width: 30, height: 30).background(jarvisCyan, in: RoundedRectangle(cornerRadius: 8))
                }
                .buttonStyle(.plain)
                .disabled(chat.draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                .help("Send")
            }
        }
        .padding(.horizontal, 10)
        .padding(.vertical, 7)
        .background(Color.white.opacity(0.045), in: RoundedRectangle(cornerRadius: 12))
        .overlay(RoundedRectangle(cornerRadius: 12).strokeBorder(Color.white.opacity(0.09)))
        .padding(.horizontal, 24)
        .padding(.top, 9)
        .padding(.bottom, 11)
        .onAppear {
            if chat.focusInputAfterOpen {
                inputFocused = true
                chat.focusInputAfterOpen = false
            }
        }
        .onChange(of: chat.focusInputAfterOpen) { shouldFocus in
            guard shouldFocus else { return }
            inputFocused = true
            chat.focusInputAfterOpen = false
        }
    }
}

private struct HUDBrackets: View {
    var color: Color = jarvisCyan
    var body: some View {
        RoundedRectangle(cornerRadius: 12).strokeBorder(color.opacity(0.22), lineWidth: 1)
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
        Ability(id: "screen", title: "Screen control", detail: "Reads, clicks, types, drags, and arranges app windows", symbol: "cursorarrow.click.2", tools: ["control_screen", "resume_task", "click_in_app", "menu_in_app", "type_in_app", "keys_in_app", "read_screen", "new_document", "paste_into_app", "send_message", "read_chat", "clipboard", "window_control", "drag_in_app"]),
        Ability(id: "web", title: "Web, YouTube & TikTok", detail: "Opens websites, searches the web and finds video examples", symbol: "magnifyingglass", tools: ["open_url", "web_search", "ask_chatgpt", "watch_video"]),
        Ability(id: "apps", title: "Applications", detail: "Finds installed and downloaded apps, shows where they are, opens and quits apps, and manages installs with confirmation", symbol: "square.grid.2x2", tools: ["list_applications", "list_running_apps", "open_app", "quit_app", "install_app", "uninstall_app"]),
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
                Toggle("Voice shortcut — tap to open Helm, hold to speak", isOn: $chat.voiceHotkey)
                Picker("Hold to talk", selection: $chat.voiceShortcut) {
                    Text("Control + Option").tag("control+option")
                    Text("Control + Shift").tag("control+shift")
                    Text("Option + Shift").tag("option+shift")
                    Text("Control + Command").tag("control+command")
                    Text("Option + Command").tag("option+command")
                }
                .disabled(!chat.voiceHotkey)
                .help("Choose the modifier keys to hold while speaking.")
                HStack(spacing: 10) {
                    Button("Test voice shortcut") { chat.testVoiceShortcut() }
                        .disabled(!chat.voiceHotkey || chat.voiceShortcutTestPending)
                    Text(chat.voiceShortcutTestStatus.isEmpty
                         ? (CGPreflightListenEventAccess() ? "Voice shortcut is ready to test." : "Control+Option+Space opens Helm without Input Monitoring.")
                         : chat.voiceShortcutTestStatus)
                        .font(.system(size: 10)).foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
                Label(chat.launchShortcutAvailable ? "Control+Option+Space is registered" : "Control+Option+Space could not register", systemImage: chat.launchShortcutAvailable ? "checkmark.circle.fill" : "exclamationmark.triangle.fill")
                    .font(.system(size: 10)).foregroundStyle(chat.launchShortcutAvailable ? Color.green : Color.orange)
                Text("Open Helm: Control+Option+Space. Tap/hold Control+Option needs Input Monitoring. Helm must be running in the menu bar.")
                    .font(.system(size: 10)).foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
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

private enum PermissionState: Equatable {
    case granted
    case needsPermission
    case notChecked
    case promptOnUse
    case limited
    case unavailable
    case disabled

    var label: String {
        switch self {
        case .granted: "Allowed"
        case .needsPermission: "Needs permission"
        case .notChecked: "Test needed"
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
        case .notChecked, .promptOnUse, .limited: .yellow
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

    var privacyPane: String? {
        switch id {
        case "microphone": "Privacy_Microphone"
        case "speech": "Privacy_SpeechRecognition"
        case "input-monitoring": "Privacy_ListenEvent"
        case "screen-recording": "Privacy_ScreenCapture"
        case "accessibility", "keyboard", "mouse": "Privacy_Accessibility"
        case "automation": "Privacy_Automation"
        default: nil
        }
    }
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
        "window_control", "drag_in_app",
    ]

    static func items(
        disabledTools: Set<String>,
        voiceHotkey: Bool,
        emergencyStopShortcutAvailable: Bool,
        pythonScreenCaptureAllowed: Bool?
    ) -> [PermissionItem] {
        let screenEnabled = !disabledTools.contains("control_screen")
        let desktopEnabled = !desktopTools.isSubset(of: disabledTools)
        let browserEnabled = !Set(["open_url", "web_search", "ask_chatgpt", "watch_video"]).isSubset(of: disabledTools)
        let appsEnabled = Set(["list_applications", "list_running_apps", "open_app", "quit_app", "install_app", "uninstall_app"]).isDisjoint(with: disabledTools)
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
                detail: CGPreflightListenEventAccess()
                    ? "Helm's live Input Monitoring preflight is allowed. Required for the modifier-only Control+Option gesture; Control+Option+Space works without it."
                    : "Helm's live preflight says this running copy is not allowed. If Settings already shows Helm on, remove that old row and add the current app at \(Bundle.main.bundleURL.path); ad-hoc rebuilds can leave a stale grant.",
                state: voiceHotkey
                    ? (CGPreflightListenEventAccess() ? .granted : .needsPermission)
                    : .disabled,
                canRequest: voiceHotkey
            ),
            PermissionItem(
                id: "screen-recording", symbol: "rectangle.dashed.badge.record", title: "Screen Recording",
                detail: pythonScreenCaptureAllowed == nil
                    ? "Run a live check to capture an actual screenshot in Helm's own process. Python receives the image through a private loopback connection."
                    : (pythonScreenCaptureAllowed == true
                        ? "Verified: Helm's native capture bridge took a screenshot and returned it to the assistant."
                        : "Helm's live screenshot failed. Check the report below for the app's Screen Recording preflight and exact capture error."),
                state: screenEnabled
                    ? (pythonScreenCaptureAllowed.map { $0 ? .granted : .needsPermission } ?? .notChecked)
                    : .disabled,
                canRequest: screenEnabled
            ),
            PermissionItem(
                id: "accessibility", symbol: "cursorarrow.click.2", title: "Accessibility control",
                detail: "Allows Helm to inspect running apps and their controls, click, type, drag, and move, resize, or close windows.",
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
                id: "automation", symbol: "bolt.horizontal.circle", title: "App automation",
                detail: "macOS asks separately the first time Helm needs to automate an app. Approval is per app.",
                state: .promptOnUse,
                canRequest: false
            ),
            PermissionItem(
                id: "files", symbol: "folder", title: "Files",
                detail: "When Files is enabled in Abilities, Helm's file tools are limited to your home folder. Deletes and moves require confirmation.",
                state: .limited,
                canRequest: false
            ),
            PermissionItem(
                id: "applications", symbol: "square.grid.2x2", title: "Applications",
                detail: appsEnabled
                    ? "App discovery shows installed and downloaded bundles and their locations. Opening a downloaded app may still require macOS approval; install and removal always need confirmation."
                    : "Application listing and management are turned off in Abilities.",
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

    /// Tests the native capture path used by desktop control, including the private bridge to Python.
    static func runLiveChecks(
        requestScreenRecording: Bool = false,
        completion: @escaping (Bool?, String) -> Void
    ) {
        DispatchQueue.global(qos: .userInitiated).async {
            let root = URL(fileURLWithPath: #filePath)
                .deletingLastPathComponent()
                .deletingLastPathComponent()
                .deletingLastPathComponent()
            let python = root.appendingPathComponent(".venv/bin/python")
            let script = root.appendingPathComponent("assistant.py")
            let appName = Bundle.main.object(forInfoDictionaryKey: "CFBundleDisplayName") as? String ?? Bundle.main.bundleIdentifier ?? "Helm"
            let bundleURL = Bundle.main.bundleURL.standardizedFileURL
            let executableURL = Bundle.main.executableURL?.standardizedFileURL
            let bundleIdentifier = Bundle.main.bundleIdentifier ?? "unknown"
            let launchServicesURL = NSWorkspace.shared.urlForApplication(withBundleIdentifier: bundleIdentifier)
            var lines = [
                "App: \(appName)",
                "Bundle ID: \(bundleIdentifier)",
                "App path: \(bundleURL.path)",
                "Bundle exists: \(FileManager.default.fileExists(atPath: bundleURL.path))",
                "Bundle executable: \(executableURL?.path ?? "missing from Info.plist")",
                "Executable exists and is executable: \(executableURL.map { FileManager.default.isExecutableFile(atPath: $0.path) } ?? false)",
                "Launch Services lookup: \(launchServicesURL?.path ?? "no registered app found for this bundle ID")",
                "Code signature: \(codeSignatureSummary())",
                "Helm app Screen Recording preflight: \(CGPreflightScreenCaptureAccess() ? "allowed" : "not allowed")",
                "Accessibility trust: \(AXIsProcessTrusted() ? "allowed" : "not allowed")",
                "Input Monitoring preflight: \(CGPreflightListenEventAccess() ? "allowed" : "not allowed")",
                "Microphone authorization: \(authorizationLabel(AVCaptureDevice.authorizationStatus(for: .audio)))",
                "Speech Recognition authorization: \(speechAuthorizationLabel(SFSpeechRecognizer.authorizationStatus()))",
            ]

            let system = AXUIElementCreateSystemWide()
            var focusedApplication: CFTypeRef?
            let axResult = AXUIElementCopyAttributeValue(
                system,
                kAXFocusedApplicationAttribute as CFString,
                &focusedApplication
            )
            lines.append("Accessibility live query: \(axResult == .success ? "focused app query succeeded" : "AX error \(axResult.rawValue)")")
            lines.append("App automation: macOS grants Apple Events per target app on first use; Helm does not claim a global grant.")
            lines.append("Capture identity: Helm app process (authenticated loopback bridge)")

            guard FileManager.default.isExecutableFile(atPath: python.path),
                  FileManager.default.fileExists(atPath: script.path) else {
                lines.append("Screenshot test: unavailable — project Python or assistant.py was not found at \(root.path)")
                DispatchQueue.main.async { completion(nil, lines.joined(separator: "\n")) }
                return
            }

            func runBackend(_ arguments: [String]) -> (Int32, String) {
                let process = Process()
                let pipe = Pipe()
                process.executableURL = python
                process.arguments = ["-u", script.path] + arguments
                process.currentDirectoryURL = root
                process.environment = ProcessInfo.processInfo.environment.merging(ScreenCaptureBridge.shared.environment) { _, new in new }
                process.standardOutput = pipe
                process.standardError = pipe
                do {
                    try process.run()
                    let output = String(decoding: pipe.fileHandleForReading.readDataToEndOfFile(), as: UTF8.self)
                    process.waitUntilExit()
                    return (process.terminationStatus, output.trimmingCharacters(in: .whitespacesAndNewlines))
                } catch {
                    return (-1, "Could not launch capture backend: \(error.localizedDescription)")
                }
            }

            if requestScreenRecording {
                lines.append("Screen Recording request was made by Helm itself; Python does not request or own this permission.")
            }
            let (status, output) = runBackend(["--test-screen-capture"])
            let capturePassed = output.contains("SCREEN_CAPTURE_OK:")
            lines.append("Helm native screenshot test through Python: \(capturePassed ? "PASSED" : "FAILED (exit \(status))")")
            if !output.isEmpty { lines.append("Capture result: \(output)") }
            DispatchQueue.main.async { completion(capturePassed, lines.joined(separator: "\n")) }
        }
    }

    private static func authorizationLabel(_ status: AVAuthorizationStatus) -> String {
        switch status {
        case .authorized: "allowed"
        case .notDetermined: "not requested yet"
        case .denied: "denied in System Settings"
        case .restricted: "restricted by macOS policy"
        @unknown default: "unknown"
        }
    }

    private static func speechAuthorizationLabel(_ status: SFSpeechRecognizerAuthorizationStatus) -> String {
        switch status {
        case .authorized: "allowed"
        case .notDetermined: "not requested yet"
        case .denied: "denied in System Settings"
        case .restricted: "restricted by macOS policy"
        @unknown default: "unknown"
        }
    }

    private static func codeSigningInfo() -> (summary: String, adHoc: Bool)? {
        var code: SecStaticCode?
        guard SecStaticCodeCreateWithPath(Bundle.main.bundleURL as CFURL, [], &code) == errSecSuccess,
              let code else { return nil }
        let validity = SecStaticCodeCheckValidity(code, [], nil) == errSecSuccess ? "valid" : "invalid or untrusted"
        var information: CFDictionary?
        guard SecCodeCopySigningInformation(code, SecCSFlags(rawValue: kSecCSSigningInformation), &information) == errSecSuccess,
              let values = information as? [String: Any] else { return nil }
        let identifier = values[kSecCodeInfoIdentifier as String] as? String ?? "unknown identifier"
        let team = values[kSecCodeInfoTeamIdentifier as String] as? String
        let flags = (values[kSecCodeInfoFlags as String] as? NSNumber)?.uint32Value ?? 0
        let adhoc = flags & 0x2 == 0x2
        return ("\(team.map { "Team \($0), " } ?? "")\(identifier), signature \(validity), ad-hoc: \(adhoc ? "yes" : "no")", adhoc)
    }

    private static func codeSignatureSummary() -> String {
        codeSigningInfo()?.summary ?? "could not inspect"
    }

    /// True when the running build is purely ad-hoc signed (no stable "Helm Dev" Keychain
    /// identity) — macOS then ties TCC grants to this exact binary's hash, so every rebuild
    /// silently invalidates Screen Recording/Input Monitoring/Accessibility until re-granted.
    /// This binary's signature never changes at runtime, so the result is cached once.
    private static var cachedAdHocSigned: Bool?

    static func isAdHocSigned() -> Bool {
        if let cachedAdHocSigned { return cachedAdHocSigned }
        let result = codeSigningInfo()?.adHoc ?? true
        cachedAdHocSigned = result
        return result
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
            openPrivacySettings("Privacy_ListenEvent")
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
        if !NSWorkspace.shared.open(url), let settingsURL = URL(string: "x-apple.systempreferences:") {
            NSWorkspace.shared.open(settingsURL)
        }
    }
}

struct PermissionsView: View {
    @EnvironmentObject private var chat: ChatController
    @State private var pythonScreenCaptureAllowed: Bool?
    @State private var isRunningLiveChecks = false
    @State private var liveCheckReport: String?

    var body: some View {
        TimelineView(.periodic(from: .now, by: 1)) { _ in
            let items = PermissionManager.items(
                disabledTools: chat.disabledTools,
                voiceHotkey: chat.voiceHotkey,
                emergencyStopShortcutAvailable: chat.emergencyStopShortcutAvailable,
                pythonScreenCaptureAllowed: pythonScreenCaptureAllowed
            )
            let missing = items.filter { $0.state == .needsPermission }.count
            ScrollView {
                VStack(alignment: .leading, spacing: 13) {
                    VStack(alignment: .leading, spacing: 9) {
                        HStack(spacing: 11) {
                            Image(systemName: "hand.raised.slash.fill").foregroundStyle(jarvisCyan)
                                .frame(width: 38, height: 38).background(jarvisCyan.opacity(0.14), in: RoundedRectangle(cornerRadius: 12))
                            VStack(alignment: .leading, spacing: 3) {
                                Text("Set up device control").font(.system(size: 15, weight: .semibold))
                                Text(missing == 0 ? "Required permissions are allowed." : "\(missing) permission\(missing == 1 ? "" : "s") still need your approval.")
                                    .font(.system(size: 11)).foregroundStyle(.secondary)
                            }
                            Spacer(minLength: 0)
                        }
                        Text("macOS has no single full-control switch. Enable Screen control in Abilities, then grant the access Helm needs below. Helm cannot grant it for you.")
                            .font(.system(size: 11)).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                        Button {
                            runLiveChecks()
                        } label: {
                            if isRunningLiveChecks {
                                Label("Testing actual permissions…", systemImage: "hourglass")
                            } else {
                                Label("Run live permission checks", systemImage: "waveform.path.ecg")
                            }
                        }
                        .buttonStyle(.borderedProminent).tint(jarvisCyan).controlSize(.small)
                        .disabled(isRunningLiveChecks)
                        HStack(spacing: 8) {
                            Button {
                                NSWorkspace.shared.activateFileViewerSelecting([Bundle.main.bundleURL])
                            } label: {
                                Label("Reveal Helm in Finder", systemImage: "folder")
                            }
                            .controlSize(.small)
                            Button("Copy app path") {
                                NSPasteboard.general.clearContents()
                                NSPasteboard.general.setString(Bundle.main.bundleURL.path, forType: .string)
                            }
                            .controlSize(.small)
                            Button("Open Screen Recording Settings") {
                                PermissionManager.openPrivacySettings("Privacy_ScreenCapture")
                            }
                            .controlSize(.small)
                        }
                        Text("If Helm is missing from the list, use + in Settings, press ⌘⇧G, and select the app bundle at the path shown in the report.")
                            .font(.system(size: 10)).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                        if let liveCheckReport {
                            VStack(alignment: .trailing, spacing: 5) {
                                Button("Copy diagnostic report") {
                                    NSPasteboard.general.clearContents()
                                    NSPasteboard.general.setString(liveCheckReport, forType: .string)
                                }
                                .controlSize(.small)
                                ScrollView {
                                    Text(liveCheckReport)
                                        .font(.system(size: 10, design: .monospaced))
                                        .textSelection(.enabled)
                                        .frame(maxWidth: .infinity, alignment: .leading)
                                        .padding(9)
                                }
                                .frame(maxHeight: 190)
                                .background(Color.black.opacity(0.22), in: RoundedRectangle(cornerRadius: 8))
                            }
                        }
                    }
                    .padding(14).background(helmSurface, in: RoundedRectangle(cornerRadius: 14))
                    .overlay(RoundedRectangle(cornerRadius: 14).strokeBorder(jarvisCyan.opacity(0.2)))

                    if PermissionManager.isAdHocSigned() {
                        VStack(alignment: .leading, spacing: 8) {
                            Label("Permissions keep resetting after every rebuild", systemImage: "exclamationmark.triangle.fill")
                                .font(.system(size: 13, weight: .semibold)).foregroundStyle(.orange)
                            Text("This build has no stable code-signing identity, so macOS ties what you grant below to this exact binary. The next time Helm is rebuilt, it becomes a different binary as far as macOS is concerned, and Screen Recording / Input Monitoring / Accessibility silently stop working — not because they were revoked, but because the new build was never granted anything.")
                                .font(.system(size: 11)).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                            Text("Fix this once, permanently: open Keychain Access → menu bar Keychain Access → Certificate Assistant → Create a Certificate. Name it exactly \"Helm Dev\", set Identity Type to Self Signed Root, and Certificate Type to Code Signing. After the next rebuild, run-menubar.sh will find it automatically and every future rebuild keeps today's grants.")
                                .font(.system(size: 11)).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                        }
                        .padding(14)
                        .background(Color.orange.opacity(0.08), in: RoundedRectangle(cornerRadius: 14))
                        .overlay(RoundedRectangle(cornerRadius: 14).strokeBorder(Color.orange.opacity(0.35)))
                    }

                    sectionTitle("SCREEN CONTROL", detail: "Screen Recording lets Helm see the display. Accessibility lets it inspect controls, click, and type.")
                    ForEach(items.filter { ["screen-recording", "accessibility"].contains($0.id) }) { permissionCard($0) }
                    sectionTitle("VOICE & SHORTCUT", detail: "Only needed for voice input and the global shortcut.")
                    ForEach(items.filter { ["microphone", "speech", "input-monitoring"].contains($0.id) }) { permissionCard($0) }
                    sectionTitle("OTHER ACCESS", detail: nil)
                    ForEach(items.filter { !["screen-recording", "accessibility", "microphone", "speech", "input-monitoring"].contains($0.id) }) { permissionCard($0) }

                    if chat.isEnabled(["control_screen"]) && !CGPreflightScreenCaptureAccess() {
                        VStack(alignment: .leading, spacing: 7) {
                            Label("Restart Helm after allowing Screen Recording", systemImage: "arrow.clockwise.circle.fill")
                                .font(.system(size: 12, weight: .semibold)).foregroundStyle(.orange)
                            Text("macOS applies this permission to the running app after it restarts.").font(.system(size: 11)).foregroundStyle(.secondary)
                            Button("Restart Helm") { ChatController.relaunch() }
                        }.padding(12).background(Color.orange.opacity(0.08), in: RoundedRectangle(cornerRadius: 12))
                    }
                }.padding(.bottom, 10)
            }
        }
    }

    private func sectionTitle(_ title: String, detail: String?) -> some View {
        VStack(alignment: .leading, spacing: 3) {
            Text(title).font(.system(size: 9, weight: .bold, design: .monospaced)).tracking(1.1).foregroundStyle(.secondary)
            if let detail { Text(detail).font(.system(size: 10)).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true) }
        }.padding(.top, 4)
    }

    private func permissionCard(_ item: PermissionItem) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 9) {
                Image(systemName: item.symbol).font(.system(size: 14)).foregroundStyle(item.state.color).frame(width: 21)
                Text(item.title).font(.system(size: 12, weight: .semibold))
                Spacer(minLength: 3)
                Text(item.state.label).font(.system(size: 9, weight: .semibold)).foregroundStyle(item.state.color)
                    .padding(.horizontal, 8).padding(.vertical, 4).background(item.state.color.opacity(0.11), in: Capsule())
            }
            Text(item.detail).font(.system(size: 10)).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            if item.state == .needsPermission && (item.canRequest || item.privacyPane != nil) {
                HStack(spacing: 8) {
                    if item.canRequest {
                        Button(item.id == "input-monitoring" ? "Enable Input Monitoring" : "Request access") {
                            if item.id == "screen-recording" {
                                PermissionManager.request(item.id)
                                runLiveChecks(requestScreenRecording: true)
                            } else {
                                PermissionManager.request(item.id)
                            }
                        }.buttonStyle(.borderedProminent).tint(jarvisCyan).controlSize(.small)
                    }
                    if let pane = item.privacyPane {
                        Button("Open System Settings") { PermissionManager.openPrivacySettings(pane) }.controlSize(.small)
                    }
                }
            }
            if item.state == .disabled {
                Text("Turn on the matching ability to use this permission.").font(.system(size: 10)).foregroundStyle(.secondary)
            }
        }
        .padding(12).frame(maxWidth: .infinity, alignment: .leading)
        .background(helmSurface, in: RoundedRectangle(cornerRadius: 12))
        .overlay(RoundedRectangle(cornerRadius: 12).strokeBorder(Color.white.opacity(0.07)))
    }

    private func runLiveChecks(requestScreenRecording: Bool = false) {
        guard !isRunningLiveChecks else { return }
        isRunningLiveChecks = true
        PermissionManager.runLiveChecks(requestScreenRecording: requestScreenRecording) { allowed, report in
            pythonScreenCaptureAllowed = allowed
            liveCheckReport = report
            isRunningLiveChecks = false
        }
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
            "Here's how Helm is set up. Tap Control+Option to open Helm, hold it to speak, or type below.",
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
        VStack(alignment: .leading, spacing: 22) {
            HStack(alignment: .center, spacing: 34) {
                ZStack {
                    Reactor(active: true, listening: false)
                    HelmBrandMark(size: 82)
                        .shadow(color: jarvisBlue.opacity(0.32), radius: 18)
                }
                .frame(width: 200, height: 200)
                .accessibilityLabel("Helm, your Mac assistant")
                VStack(alignment: .leading, spacing: 12) {
                    feature("waveform", "Talk to it", "Tap Control+Option to open Helm; hold it to speak")
                    feature("cursorarrow.click.2", "Works your Mac", "Opens apps, clicks and types for you")
                    feature("magnifyingglass", "Searches for you", "The web, YouTube and TikTok")
                    feature("music.note", "Runs the room", "Music, volume, time and battery")
                    feature("key", "Your choice of brain", "Local, Claude, ChatGPT, Gemini or OpenRouter")
                }
            }
            VStack(alignment: .leading, spacing: 11) {
                Text("QUICK START")
                    .font(.system(size: 11, weight: .semibold, design: .monospaced))
                    .tracking(2)
                    .foregroundStyle(.secondary)
                tutorialStep("1", "Choose your setup", "Pick an AI brain, then enable only the abilities you need.")
                tutorialStep("2", "Give Helm a task", "Type a request in the chat, or use the microphone to dictate into your draft.")
                tutorialStep("3", "Review before it acts", "Approve proposed actions. Sensitive actions always need your confirmation; Stop ends a task.")
                tutorialStep("4", "Try a first task", "For example: “Open Safari” or “Find three beginner SwiftUI tutorials.”")
                Text("Press Control+Option+Space to bring Helm forward. Grant macOS permissions only for abilities you choose to use.")
                    .font(.system(size: 11))
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                    .padding(.top, 2)
                Text("To launch Helm later, double-click Open Helm.command or run bash run-menubar.sh from the project folder.")
                    .font(.system(size: 11))
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    private func tutorialStep(_ number: String, _ title: String, _ detail: String) -> some View {
        HStack(alignment: .top, spacing: 11) {
            Text(number)
                .font(.system(size: 11, weight: .semibold, design: .monospaced))
                .foregroundStyle(jarvisCyan)
                .frame(width: 18, alignment: .leading)
            VStack(alignment: .leading, spacing: 2) {
                Text(title).font(.system(size: 12, weight: .semibold))
                Text(detail)
                    .font(.system(size: 11))
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
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
            summaryRow("waveform", "Voice", chat.voiceHotkey ? "Tap to open, hold to speak" : "Mic button only")
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
        ("Agent 1", "agent1.log"), ("Agent 2", "agent2.log"), ("Agent 3", "agent3.log"),
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
    private let pages: [(Int, String, String)] = [
        (0, "Brain", "brain.head.profile"),
        (1, "Abilities", "square.grid.2x2"),
        (2, "Permissions", "hand.raised"),
        (3, "WhatsApp", "message.fill"),
        (4, "Telegram", "paperplane.fill"),
        (5, "Discord", "bubble.left.and.bubble.right.fill"),
        (6, "Slack", "number"),
        (7, "Logs", "doc.text.magnifyingglass"),
    ]

    var body: some View {
        VStack(spacing: 0) {
            HStack(spacing: 0) {
                ScrollView {
                    VStack(alignment: .leading, spacing: 6) {
                        Text("GENERAL").font(.system(size: 9, weight: .semibold, design: .monospaced)).foregroundStyle(.secondary).padding(.horizontal, 8).padding(.top, 5)
                        ForEach(pages.filter { $0.0 < 3 }, id: \.0) { pageRow($0) }
                        Text("INTEGRATIONS").font(.system(size: 9, weight: .semibold, design: .monospaced)).foregroundStyle(.secondary).padding(.horizontal, 8).padding(.top, 14)
                        ForEach(pages.filter { (3...6).contains($0.0) }, id: \.0) { pageRow($0) }
                        Text("ACTIVITY").font(.system(size: 9, weight: .semibold, design: .monospaced)).foregroundStyle(.secondary).padding(.horizontal, 8).padding(.top, 14)
                        ForEach(pages.filter { $0.0 == 7 }, id: \.0) { pageRow($0) }
                    }
                    .padding(12)
                }
                .frame(width: 175)
                .background(Color.white.opacity(0.025))
                Rectangle().fill(jarvisCyan.opacity(0.14)).frame(width: 1)
                ScrollView {
                    VStack(alignment: .leading, spacing: 14) {
                        HStack {
                            Text(pages.first(where: { $0.0 == tab })?.1 ?? "Settings")
                                .font(.system(size: 20, weight: .semibold))
                            Spacer()
                            if let bridgeStatus {
                                Label(bridgeStatus, systemImage: bridgeStatus == "Running" ? "checkmark.circle.fill" : "circle.dashed")
                                    .font(.system(size: 10, weight: .medium))
                                    .foregroundStyle(bridgeStatus == "Running" ? .green : .secondary)
                            }
                        }
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
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(22)
                }
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
        .frame(width: 820, height: 660)
        .onAppear {
            if let requested = chat.requestedSettingsTab {
                tab = requested
                chat.requestedSettingsTab = nil
            }
        }
    }

    private var bridgeStatus: String? {
        let value: String?
        switch tab {
        case 3: value = chat.whatsappBridgeStatus
        case 4: value = chat.telegramBridgeStatus
        case 5: value = chat.discordBridgeStatus
        case 6: value = chat.slackBridgeStatus
        default: value = nil
        }
        guard let value else { return nil }
        if value.hasPrefix("Running") { return "Running" }
        if value == "Not configured" || value == "Stopped" { return "Setup needed" }
        return "Check status"
    }

    private func pageRow(_ page: (Int, String, String)) -> some View {
        Button {
            withAnimation(.easeInOut(duration: 0.16)) { tab = page.0 }
        } label: {
            HStack(spacing: 9) {
                Image(systemName: page.2).frame(width: 16)
                Text(page.1).lineLimit(1)
                Spacer(minLength: 0)
            }
            .font(.system(size: 11, weight: tab == page.0 ? .semibold : .regular))
            .foregroundStyle(tab == page.0 ? jarvisCyan : .primary)
            .padding(.horizontal, 9)
            .padding(.vertical, 8)
            .background(tab == page.0 ? jarvisCyan.opacity(0.12) : .clear)
            .clipShape(RoundedRectangle(cornerRadius: 7))
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
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
        "Agent 1": Color(red: 0.55, green: 0.67, blue: 0.88),
        "Agent 2": Color(red: 0.83, green: 0.67, blue: 0.45),
        "Agent 3": Color(red: 0.49, green: 0.70, blue: 0.59),
    ]

    @Published var cursors: [CursorState] = []
    @Published var mouse = CGPoint(x: -200, y: -200)
    @Published var buddyText = ""
    @Published var buddyEnabled = UserDefaults.standard.object(forKey: "cursorBuddy") as? Bool ?? false {
        didSet {
            UserDefaults.standard.set(buddyEnabled, forKey: "cursorBuddy")
            if !buddyEnabled {
                buddyText = ""
                stopFollowing()
            }
        }
    }
    private var panel: NSPanel?
    private var hideTasks: [String: DispatchWorkItem] = [:]
    private var followTimer: Timer?
    private var screenHeight: CGFloat = 0

    func start() {
        ensurePanel()
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
        if !action.isEmpty { say("\(id) · \(action)") }
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
        guard buddyEnabled else { return }
        buddyText = String(text.prefix(72))
        startFollowing()
        hideTasks["buddy"]?.cancel()
        let task = DispatchWorkItem { [weak self] in
            guard let self else { return }
            self.buddyText = ""
            self.stopFollowing()
        }
        hideTasks["buddy"] = task
        DispatchQueue.main.asyncAfter(deadline: .now() + 6, execute: task)
    }

    private func startFollowing() {
        guard followTimer == nil else { return }
        followTimer = Timer.scheduledTimer(withTimeInterval: 1.0 / 20, repeats: true) { [weak self] _ in
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
        GeometryReader { geometry in
            ZStack(alignment: .topLeading) {
                Color.clear
                ForEach(overlay.cursors) { cursor in OneCursor(cursor: cursor, screenSize: geometry.size) }
                if overlay.buddyEnabled && !overlay.buddyText.isEmpty {
                    Buddy(text: overlay.buddyText, busy: overlay.cursors.contains { $0.visible })
                        .frame(width: 252, height: 48, alignment: .leading)
                        .offset(
                            x: min(max(overlay.mouse.x < geometry.size.width * 0.65 ? overlay.mouse.x + 20 : overlay.mouse.x - 272,
                                       8), max(8, geometry.size.width - 260)),
                            y: min(max(overlay.mouse.y < geometry.size.height * 0.68 ? overlay.mouse.y + 22 : overlay.mouse.y - 56,
                                       8), max(8, geometry.size.height - 54))
                        )
                }
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        }
        .allowsHitTesting(false)
    }
}

private struct OneCursor: View {
    let cursor: CursorState
    let screenSize: CGSize
    @State private var ripple = false

    var body: some View {
        ZStack(alignment: .topLeading) {
            Circle()
                .stroke(cursor.color.opacity(0.8), lineWidth: 1.5)
                .frame(width: ripple ? 34 : 7, height: ripple ? 34 : 7)
                .opacity(ripple ? 0 : 0.75)
                .position(cursor.position)
            CursorArrow()
                .fill(cursor.color)
                .overlay(CursorArrow().stroke(Color.black.opacity(0.65), lineWidth: 1))
                .frame(width: 15, height: 20)
                .shadow(color: .black.opacity(0.45), radius: 2, y: 1)
                .offset(x: cursor.position.x - 2, y: cursor.position.y - 2)
            HStack(spacing: 6) {
                RoundedRectangle(cornerRadius: 1).fill(cursor.color).frame(width: 2, height: 23)
                VStack(alignment: .leading, spacing: 2) {
                    Text(cursor.id)
                        .font(.system(size: 8, weight: .semibold))
                        .foregroundStyle(.white.opacity(0.66))
                    if !cursor.action.isEmpty {
                        Text(cursor.action)
                            .font(.system(size: 10, weight: .medium))
                            .foregroundStyle(.white.opacity(0.94))
                            .lineLimit(1)
                    }
                }
                .frame(maxWidth: 156, alignment: .leading)
            }
            .padding(.horizontal, 8).padding(.vertical, 6)
            .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 8, style: .continuous))
            .overlay(RoundedRectangle(cornerRadius: 8, style: .continuous).strokeBorder(Color.white.opacity(0.13)))
            .shadow(color: .black.opacity(0.30), radius: 8, y: 3)
            .offset(
                x: min(max(cursor.position.x > screenSize.width - 190 ? cursor.position.x - 190 : cursor.position.x + 16, 6), max(6, screenSize.width - 184)),
                y: min(max(cursor.position.y > screenSize.height - 48 ? cursor.position.y - 47 : cursor.position.y + 13, 6), max(6, screenSize.height - 42))
            )
        }
        .frame(width: screenSize.width, height: screenSize.height, alignment: .topLeading)
        .animation(.easeInOut(duration: 0.28), value: cursor.position)
        .opacity(cursor.visible ? 1 : 0)
        .animation(.easeOut(duration: 0.3), value: cursor.visible)
        .onChange(of: cursor.clicks) { _ in
            ripple = false
            withAnimation(.easeOut(duration: 0.5)) { ripple = true }
        }
    }
}

/// Quiet task-status pill beside the pointer. It never intercepts clicks.
private struct Buddy: View {
    let text: String
    let busy: Bool

    var body: some View {
        HStack(spacing: 10) {
            ZStack {
                RoundedRectangle(cornerRadius: 10, style: .continuous)
                    .fill(LinearGradient(colors: [jarvisCyan.opacity(0.30), jarvisBlue.opacity(0.16)],
                                         startPoint: .topLeading, endPoint: .bottomTrailing))
                Image(systemName: busy ? "sparkles" : "sparkle")
                    .font(.system(size: 13, weight: .semibold))
                    .foregroundStyle(jarvisCyan)
            }
            .frame(width: 30, height: 30)
            VStack(alignment: .leading, spacing: 3) {
                HStack(spacing: 5) {
                    Circle().fill(busy ? jarvisCyan : Color.white.opacity(0.42)).frame(width: 5, height: 5)
                    Text(busy ? "HELM · IN PROGRESS" : "HELM")
                        .font(.system(size: 8, weight: .bold, design: .monospaced))
                        .tracking(0.7).foregroundStyle(.secondary)
                }
                Text(text)
                    .font(.system(size: 10, weight: .medium))
                    .foregroundStyle(.primary.opacity(0.94))
                    .lineLimit(1)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
        }
        .padding(.horizontal, 10).padding(.vertical, 8)
        .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 11, style: .continuous))
        .overlay(RoundedRectangle(cornerRadius: 11, style: .continuous).strokeBorder(Color.white.opacity(0.15)))
        .shadow(color: .black.opacity(0.24), radius: 10, y: 3)
        .animation(.easeOut(duration: 0.20), value: text)
        .accessibilityLabel(busy ? "Helm is working: \(text)" : "Helm cursor companion")
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


// MARK: - Voice toast

/// A small card at the top-right of the screen showing what you said and Helm's answer, for when
/// you talk (Control+Option) while the HUD window isn't open — otherwise the reply only ever
/// landed in a chat transcript nobody was looking at.
final class VoiceToast: ObservableObject {
    static let shared = VoiceToast()

    @Published fileprivate var you = ""
    @Published fileprivate var helm = ""
    @Published fileprivate var visible = false
    private var panel: NSPanel?
    private var hideTask: DispatchWorkItem?

    func show(you text: String) {
        ensurePanel()
        you = text
        helm = ""
        visible = true
        scheduleHide(after: 20)  // long enough to read, in case the answer takes a moment
    }

    func answer(_ text: String) {
        guard visible else { return }  // the HUD was opened (or the toast dismissed) meanwhile
        helm = text
        scheduleHide(after: 12)
    }

    func dismiss() {
        hideTask?.cancel()
        withAnimation(.easeOut(duration: 0.2)) { visible = false }
    }

    private func scheduleHide(after seconds: TimeInterval) {
        hideTask?.cancel()
        let task = DispatchWorkItem { [weak self] in
            withAnimation(.easeOut(duration: 0.3)) { self?.visible = false }
        }
        hideTask = task
        DispatchQueue.main.asyncAfter(deadline: .now() + seconds, execute: task)
    }

    private func ensurePanel() {
        guard panel == nil, let screen = NSScreen.screens.first else { return }
        let overlay = NSPanel(contentRect: screen.frame, styleMask: [.borderless, .nonactivatingPanel],
                              backing: .buffered, defer: false)
        overlay.isOpaque = false
        overlay.backgroundColor = .clear
        overlay.hasShadow = false
        overlay.ignoresMouseEvents = true  // never blocks the user's own clicks; it only auto-dismisses
        overlay.level = .screenSaver
        overlay.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .stationary, .ignoresCycle]
        overlay.contentView = NSHostingView(rootView: VoiceToastView().environmentObject(self))
        overlay.orderFrontRegardless()
        panel = overlay
    }
}

private struct VoiceToastView: View {
    @EnvironmentObject private var toast: VoiceToast

    var body: some View {
        VStack(alignment: .trailing, spacing: 0) {
            if toast.visible {
                VStack(alignment: .leading, spacing: 8) {
                    if !toast.you.isEmpty {
                        Text(toast.you)
                            .font(.system(size: 12, weight: .medium, design: .rounded))
                            .foregroundStyle(.white.opacity(0.75))
                    }
                    if !toast.helm.isEmpty {
                        Text(toast.helm)
                            .font(.system(size: 13, weight: .semibold, design: .rounded))
                            .foregroundStyle(.white)
                    } else {
                        HStack(spacing: 5) {
                            ProgressView().controlSize(.small).tint(jarvisCyan)
                            Text("Thinking…").font(.system(size: 12)).foregroundStyle(.white.opacity(0.6))
                        }
                    }
                }
                .frame(maxWidth: 280, alignment: .leading)
                .padding(14)
                .background(Color.black.opacity(0.88), in: RoundedRectangle(cornerRadius: 14))
                .overlay(RoundedRectangle(cornerRadius: 14).strokeBorder(jarvisCyan.opacity(0.5)))
                .shadow(color: .black.opacity(0.4), radius: 12, y: 4)
                .padding(.top, 44)
                .padding(.trailing, 18)
                .transition(.move(edge: .top).combined(with: .opacity))
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topTrailing)
        .allowsHitTesting(false)
    }
}

// MARK: - Background agents

/// One background agent: its own Helm engine process, conversation file, cursor colour and approvals.
@MainActor
final class AgentRun: ObservableObject, Identifiable {
    static let roles = ["researcher", "operator", "verifier"]
    let number: Int
    var id: String { "Agent \(number)" }
    var role: String {
        switch number {
        case 1: "researcher"
        case 2: "operator"
        default: "verifier"
        }
    }
    var roleLabel: String { role.capitalized }
    var roleDescription: String {
        switch role {
        case "researcher": "Find evidence and cite sources"
        case "operator": "Carry out the approved task"
        default: "Check results and report gaps"
        }
    }
    var color: Color { AgentCursor.colors[id] ?? jarvisCyan }
    @Published var task = ""
    @Published var plan: [String] = []
    @Published var activity: [String] = []
    @Published var reply = ""
    @Published var pendingApproval: String?
    @Published var isRunning = false
    @Published var failed = false
    @Published var completed = false
    @Published var stoppedByUser = false
    @Published private(set) var startedAt: Date?
    @Published private(set) var finishedAt: Date?
    fileprivate var workflowID: String?
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

    /// Mirrors the main chat's backend.log, so a failed or misbehaving agent can actually be
    /// reviewed afterward instead of only showing the last 3 lines in its lane, then losing
    /// everything on the next run.
    private var logFilename: String { "agent\(number).log" }

    private func log(_ text: String) {
        _ = ChatController.appendToHelmLog(filename: logFilename, text: text)
    }

    func run(_ text: String, workflowID: String? = nil, displayTask: String? = nil) {
        guard let owner, !isRunning else { return }
        self.workflowID = workflowID
        task = displayTask ?? text
        plan = []
        activity = ["Starting…"]
        reply = ""
        failed = false
        completed = false
        stoppedByUser = false
        startedAt = Date()
        finishedAt = nil
        log("\n=== \(Date()) task: \(text)\n")
        let child = Process()
        let output = Pipe()
        let input = Pipe()
        child.executableURL = owner.projectRoot.appendingPathComponent(".venv/bin/python")
        child.arguments = ["-u", owner.projectRoot.appendingPathComponent("assistant.py").path]
            + owner.engineArguments(for: text, history: historyURL, agentRole: role)
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
            finishedAt = Date()
            owner.agentDidFinish(self, status: 1)
        }
    }

    func approve(all: Bool = false) { respond(all ? "all\n" : "y\n") }
    func reject() { respond("n\n") }

    func stop() {
        pendingApproval = nil
        stoppedByUser = true
        note("Stopped by you. You can resume or take over this task.")
        process?.terminate()
    }

    func resume() {
        guard !task.isEmpty, !isRunning, stoppedByUser || failed else { return }
        run("Continue the previous task from its saved conversation. First inspect what has already been completed, do not repeat irreversible actions, and continue only the remaining steps. Report what you verify and what remains.", workflowID: workflowID, displayTask: task)
    }

    func takeOver() {
        owner?.takeOver(from: self)
    }

    func openLog() {
        let url = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Logs/Helm/\(logFilename)")
        NSWorkspace.shared.open(url)
    }

    private func respond(_ answer: String) {
        guard let inputPipe, pendingApproval != nil else { return }
        inputPipe.fileHandleForWriting.write(Data(answer.utf8))
        pendingApproval = nil
        if answer != "n\n" { NSApp.deactivate() }  // let the agent's target app come forward
    }

    private func receive(_ text: String) {
        log(text)
        buffer += text
        let parts = buffer.components(separatedBy: .newlines)
        buffer = parts.last ?? ""
        for line in parts.dropLast() where !line.trimmingCharacters(in: .whitespaces).isEmpty {
            if line.hasPrefix("APPROVAL_REQUIRED: ") {
                pendingApproval = String(line.dropFirst("APPROVAL_REQUIRED: ".count))
            } else if line.hasPrefix("TASK_PLAN_JSON:") {
                let data = Data(line.dropFirst("TASK_PLAN_JSON:".count).utf8)
                if let decoded = try? JSONDecoder().decode([String].self, from: data), !decoded.isEmpty {
                    plan = decoded
                    note("Plan ready · \(decoded.count) checkpoints")
                    owner?.recordPlan(decoded, from: self)
                } else {
                    note("Could not read the task plan.")
                    failed = true
                }
            } else if line.hasPrefix("RESEARCH_SOURCE_JSON:") {
                let data = Data(line.dropFirst("RESEARCH_SOURCE_JSON:".count).utf8)
                if let source = try? JSONDecoder().decode(ResearchSource.self, from: data) {
                    owner?.recordSource(source, from: self)
                    note("Source saved · \(source.title)")
                }
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
                // Only step aside for tools that actually need another app frontmost — matching
                // the main chat's behavior, instead of deactivating Helm on every tool call,
                // which was needlessly yanking focus while agents ran "in the background."
                if ChatController.actingTools.contains(tool) { NSApp.deactivate() }
                note(ChatController.toolLabels[tool] ?? tool.replacingOccurrences(of: "_", with: " "))
            } else if line.hasPrefix("RESULT: ") {
                note(String(line.dropFirst("RESULT: ".count)))
            } else if line.hasPrefix("Error:") {
                note(line)
                failed = true
            } else if !line.hasPrefix("RESULT: ") && !line.hasPrefix("Allow this action?") {
                note(line)
            }
        }
    }

    private func note(_ text: String) {
        activity = Array((activity + [String(text.prefix(400))]).suffix(40))
        owner?.recordAgentActivity(text, from: self)
    }

    private func finish(status: Int32) {
        finishedAt = Date()
        if stoppedByUser {
            note("Stopped by you. Resume continues from the saved agent conversation.")
        } else if status != 0 && reply.isEmpty {
            failed = true
            note("Run failed with exit code \(status). Review the activity and logs before resuming.")
        } else {
            completed = status == 0 && !failed
            if reply.isEmpty { note("Process finished without a final report. Review activity before treating the task as complete.") }
            else { note("Run finished. Review the agent report and activity before relying on the result.") }
        }
        log("\n=== \(Date()) exited with status \(status)\n")
        process = nil
        inputPipe = nil
        pendingApproval = nil
        isRunning = false
        owner?.agentDidFinish(self, status: status)
    }
}

/// Task board: show the user's goal and the agent's current work before decorative activity.
struct AgentsBoard: View {
    @EnvironmentObject private var chat: ChatController
    @State private var isExpanded = false
    private var activeCount: Int { chat.agents.filter(\.isRunning).count }
    private var attentionCount: Int { chat.agents.filter { $0.pendingApproval != nil }.count }

    var body: some View {
        VStack(alignment: .leading, spacing: 9) {
            HStack(spacing: 8) {
                Button { withAnimation(.easeInOut(duration: 0.18)) { isExpanded.toggle() } } label: {
                    HStack(spacing: 7) {
                        Image(systemName: "sparkles").foregroundStyle(jarvisCyan)
                        Text("Agent team").font(.system(size: 11, weight: .semibold))
                        Text("\(activeCount) active · \(attentionCount) approvals")
                            .font(.system(size: 9)).foregroundStyle(.secondary)
                        Image(systemName: "chevron.down")
                            .font(.system(size: 8, weight: .semibold)).foregroundStyle(.tertiary)
                            .rotationEffect(.degrees(isExpanded ? 180 : 0))
                    }
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .accessibilityLabel("\(isExpanded ? "Hide" : "Show") agent team, \(activeCount) active, \(attentionCount) approvals")
                Spacer()
                if activeCount > 0 {
                    Button("Stop all") { chat.agents.filter(\.isRunning).forEach { $0.stop() } }
                        .font(.system(size: 9, weight: .medium)).foregroundStyle(.secondary).buttonStyle(.plain)
                }
                if !chat.draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                    Button { chat.dispatchDraft() } label: {
                        Label("Run with team", systemImage: "arrow.turn.down.right")
                            .font(.system(size: 9, weight: .semibold))
                    }
                    .buttonStyle(.borderedProminent)
                    .tint(jarvisCyan)
                    .controlSize(.mini)
                    .disabled(activeCount > 0 || attentionCount > 0)
                    .help("Research, perform, and verify this task with Helm's three agents")
                }
            }
            if isExpanded {
                HStack(alignment: .top, spacing: 8) {
                    ForEach(chat.agents) { agent in
                        AgentLane(agent: agent, hasDraft: !chat.draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty) {
                            chat.dispatchDraft(to: agent)
                        }
                        .environmentObject(chat)
                    }
                }
                if let workflow = chat.workflowRecord {
                    WorkflowProgressCard(record: workflow, resume: { chat.resumeWorkflow() })
                }
            }
        }
        .padding(.horizontal, 1)
        .padding(.top, 8)
        .onChange(of: activeCount) { count in if count > 0 { isExpanded = true } }
        .onChange(of: attentionCount) { count in if count > 0 { isExpanded = true } }
    }
}

private struct WorkflowProgressCard: View {
    let record: AgentWorkflowRecord
    let resume: () -> Void

    private var tint: Color {
        switch record.status {
        case "completed": .green
        case "blocked", "interrupted": .orange
        default: jarvisCyan
        }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 9) {
            HStack(spacing: 7) {
                Image(systemName: record.status == "completed" ? "checkmark.seal.fill" : "rectangle.3.group")
                    .foregroundStyle(tint)
                Text("Task workflow").font(.system(size: 11, weight: .semibold))
                Text(record.status.capitalized).font(.system(size: 9, weight: .medium)).foregroundStyle(tint)
                Spacer()
                if ["interrupted", "blocked"].contains(record.status) {
                    Button("Resume workflow", action: resume).buttonStyle(.borderedProminent).tint(jarvisCyan).controlSize(.small)
                }
            }
            Text(record.task).font(.system(size: 10, weight: .medium)).lineLimit(2)
            HStack(alignment: .top, spacing: 8) {
                ForEach(record.checkpoints) { checkpoint in
                    VStack(alignment: .leading, spacing: 4) {
                        HStack(spacing: 4) {
                            Image(systemName: checkpoint.status == "done" ? "checkmark.circle.fill" : checkpoint.status == "in_progress" ? "circle.dotted" : checkpoint.status == "interrupted" || checkpoint.status == "blocked" ? "exclamationmark.circle.fill" : "circle")
                            Text(checkpoint.title).lineLimit(2)
                        }
                        .font(.system(size: 9, weight: .medium))
                        .foregroundStyle(checkpoint.status == "done" ? Color.green : checkpoint.status == "interrupted" || checkpoint.status == "blocked" ? Color.orange : .secondary)
                        if !checkpoint.evidence.isEmpty {
                            Text(checkpoint.evidence).font(.system(size: 8)).foregroundStyle(.secondary).lineLimit(2)
                        }
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                }
            }
            if !record.sources.isEmpty {
                HStack(spacing: 5) {
                    Image(systemName: "link").foregroundStyle(.secondary)
                    Text("Sources:").foregroundStyle(.secondary)
                    ForEach(Array(record.sources.prefix(4).enumerated()), id: \.offset) { index, source in
                        if let url = URL(string: source.url) {
                            Link("[\(index + 1)] \(source.title)", destination: url).lineLimit(1)
                        }
                    }
                    if record.sources.count > 4 { Text("+\(record.sources.count - 4)").foregroundStyle(.secondary) }
                }.font(.system(size: 8))
            }
            if record.status == "completed" {
                Label("Verification report is included in the final response. Review the evidence before relying on it.", systemImage: "checkmark.shield")
                    .font(.system(size: 9)).foregroundStyle(.secondary)
            }
        }
        .padding(10)
        .background(helmSurface, in: RoundedRectangle(cornerRadius: 9))
        .overlay(RoundedRectangle(cornerRadius: 9).strokeBorder(tint.opacity(0.25)))
    }
}

private struct AgentLane: View {
    @ObservedObject var agent: AgentRun
    @EnvironmentObject private var chat: ChatController
    let hasDraft: Bool
    let assignDraft: () -> Void
    @State private var showingDetails = false

    private var status: (String, Color, String) {
        if agent.pendingApproval != nil { return ("Needs approval", .orange, "hand.raised.fill") }
        if agent.isRunning { return ("Running", jarvisCyan, "circle.dotted") }
        if agent.failed { return ("Failed", .red, "xmark.circle.fill") }
        if agent.stoppedByUser { return ("Stopped", .secondary, "stop.circle") }
        if agent.completed { return ("Finished · review", .green, "checkmark.circle.fill") }
        return ("Ready", .secondary, "circle")
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 7) {
                Image(systemName: status.2).font(.system(size: 11, weight: .semibold)).foregroundStyle(status.1)
                VStack(alignment: .leading, spacing: 1) {
                    Text(agent.roleLabel).font(.system(size: 11, weight: .semibold))
                    Text(agent.roleDescription).font(.system(size: 9)).foregroundStyle(.secondary).lineLimit(1)
                }
                Spacer(minLength: 3)
                Text(status.0).font(.system(size: 9, weight: .medium)).foregroundStyle(status.1).lineLimit(1)
            }
            Text(agent.task.isEmpty ? "Ready for a task" : agent.task)
                .font(.system(size: 11, weight: .medium)).lineLimit(3).frame(maxWidth: .infinity, minHeight: 40, alignment: .topLeading)
                .foregroundStyle(agent.task.isEmpty ? Color.secondary : Color.primary.opacity(0.9))
            if !agent.plan.isEmpty {
                VStack(alignment: .leading, spacing: 3) {
                    Text("PLAN").font(.system(size: 8, weight: .bold, design: .monospaced)).tracking(1).foregroundStyle(agent.color)
                    ForEach(Array(agent.plan.prefix(3).enumerated()), id: \.offset) { index, step in
                        HStack(alignment: .top, spacing: 5) {
                            Text("\(index + 1)").foregroundStyle(agent.color).monospacedDigit()
                            Text(step).foregroundStyle(.secondary).lineLimit(2)
                        }.font(.system(size: 9))
                    }
                    if agent.plan.count > 3 { Text("+\(agent.plan.count - 3) more steps").font(.system(size: 9)).foregroundStyle(.tertiary) }
                }
            }
            if let action = agent.pendingApproval {
                VStack(alignment: .leading, spacing: 6) {
                    Label("Approval needed", systemImage: "hand.raised.fill").font(.system(size: 10, weight: .semibold)).foregroundStyle(.orange)
                    Text(action).font(.system(size: 10)).textSelection(.enabled).fixedSize(horizontal: false, vertical: true)
                    HStack(spacing: 7) {
                        Button("Allow once") { agent.approve() }.buttonStyle(.borderedProminent).tint(.orange).controlSize(.small)
                        Button("Deny") { agent.reject() }.buttonStyle(.bordered).controlSize(.small)
                    }
                }
                .padding(8).background(Color.orange.opacity(0.08), in: RoundedRectangle(cornerRadius: 8))
            } else if agent.isRunning {
                HStack(spacing: 7) {
                    ProgressView().controlSize(.small).tint(agent.color)
                    Text(agent.activity.last ?? "Starting task…").font(.system(size: 10)).foregroundStyle(.secondary).lineLimit(2)
                    Spacer(minLength: 0)
                }
            } else if !agent.reply.isEmpty {
                VStack(alignment: .leading, spacing: 4) {
                    Text("AGENT REPORT · REVIEW BEFORE RELYING ON IT")
                        .font(.system(size: 8, weight: .bold, design: .monospaced)).tracking(0.4).foregroundStyle(status.1)
                    Text(agent.reply).font(.system(size: 10)).foregroundStyle(.secondary).lineLimit(4)
                }
            } else if hasDraft && !agent.isRunning {
                Button(action: assignDraft) {
                    Label("Assign current task", systemImage: "arrow.turn.down.right")
                        .font(.system(size: 10, weight: .medium)).frame(maxWidth: .infinity, alignment: .leading)
                }.buttonStyle(.plain).foregroundStyle(agent.color).disabled(!hasDraft)
            } else if !agent.activity.isEmpty {
                Text(agent.activity.last ?? "").font(.system(size: 9)).foregroundStyle(.secondary).lineLimit(2)
            } else {
                Text("Type a task below to assign it.").font(.system(size: 9)).foregroundStyle(.tertiary).lineLimit(2)
            }
            Divider().opacity(0.45)
            HStack(spacing: 10) {
                Button("Details") { showingDetails = true }.buttonStyle(.plain).font(.system(size: 9, weight: .medium))
                Spacer(minLength: 0)
                if agent.isRunning {
                    Button("Take over") { agent.takeOver() }.buttonStyle(.plain).font(.system(size: 9, weight: .medium)).foregroundStyle(jarvisCyan)
                    Button("Stop") { agent.stop() }.buttonStyle(.plain).font(.system(size: 9, weight: .medium)).foregroundStyle(.red)
                } else if !agent.task.isEmpty {
                    Button("Take over") { agent.takeOver() }.buttonStyle(.plain).font(.system(size: 9, weight: .medium)).foregroundStyle(jarvisCyan)
                    if agent.stoppedByUser || agent.failed {
                        Button("Resume") { agent.resume() }.buttonStyle(.plain).font(.system(size: 9, weight: .medium)).foregroundStyle(agent.color)
                            .disabled(agent.isRunning || agent.pendingApproval != nil)
                    }
                }
            }
        }
        .padding(10).frame(maxWidth: .infinity, minHeight: 210, alignment: .topLeading)
        .background(Color.white.opacity(0.035), in: RoundedRectangle(cornerRadius: 10))
        .overlay(RoundedRectangle(cornerRadius: 10)
            .strokeBorder(agent.color.opacity(agent.isRunning || agent.pendingApproval != nil ? 0.3 : 0.09)))
        .sheet(isPresented: $showingDetails) {
            AgentTaskDetail(agent: agent, onTakeOver: { chat.takeOver(from: agent) })
                .environmentObject(chat)
                .preferredColorScheme(.dark)
        }
    }
}

private struct AgentTaskDetail: View {
    @ObservedObject var agent: AgentRun
    @EnvironmentObject private var chat: ChatController
    @Environment(\.dismiss) private var dismiss
    let onTakeOver: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack {
                VStack(alignment: .leading, spacing: 4) {
                    Text("\(agent.roleLabel) task").font(.system(size: 18, weight: .semibold))
                    Text(agent.task.isEmpty ? "No task assigned" : agent.task).font(.system(size: 12)).foregroundStyle(.secondary).textSelection(.enabled)
                }
                Spacer()
                Text(agent.isRunning ? "Running" : agent.failed ? "Failed" : agent.stoppedByUser ? "Stopped" : agent.completed ? "Finished · review" : "Ready")
                    .font(.system(size: 10, weight: .semibold)).foregroundStyle(agent.failed ? .red : agent.pendingApproval != nil ? .orange : jarvisCyan)
            }
            if !agent.plan.isEmpty {
                VStack(alignment: .leading, spacing: 7) {
                    Text("PLAN").font(.system(size: 9, weight: .bold, design: .monospaced)).tracking(1).foregroundStyle(.secondary)
                    ForEach(Array(agent.plan.enumerated()), id: \.offset) { index, step in
                        HStack(alignment: .top, spacing: 8) {
                            Text("\(index + 1)").font(.system(size: 10, weight: .semibold)).foregroundStyle(agent.color).frame(width: 18)
                            Text(step).font(.system(size: 11)).textSelection(.enabled)
                        }
                    }
                }.padding(12).background(helmSurface, in: RoundedRectangle(cornerRadius: 10))
            }
            if !agent.reply.isEmpty {
                VStack(alignment: .leading, spacing: 6) {
                    Label("Agent report · verify before relying on it", systemImage: "doc.text.magnifyingglass")
                        .font(.system(size: 11, weight: .semibold)).foregroundStyle(jarvisCyan)
                    Text(agent.reply).font(.system(size: 12)).textSelection(.enabled).fixedSize(horizontal: false, vertical: true)
                }.padding(12).background(helmSurface, in: RoundedRectangle(cornerRadius: 10))
            }
            Text("ACTIVITY · MOST RECENT FIRST").font(.system(size: 9, weight: .bold, design: .monospaced)).tracking(1).foregroundStyle(.secondary)
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 0) {
                    ForEach(Array(agent.activity.reversed().enumerated()), id: \.offset) { _, entry in
                        HStack(alignment: .top, spacing: 9) {
                            Circle().fill(agent.color.opacity(0.75)).frame(width: 6, height: 6).padding(.top, 5)
                            Text(entry).font(.system(size: 11)).textSelection(.enabled).frame(maxWidth: .infinity, alignment: .leading)
                        }.padding(.vertical, 7)
                        Divider().opacity(0.35)
                    }
                }
            }.frame(minHeight: 130)
            HStack {
                if agent.isRunning { Button("Stop task", role: .destructive) { agent.stop() } }
                Button("Open full activity log") { agent.openLog() }
                Button("Take over in Helm") { onTakeOver(); dismiss() }.buttonStyle(.borderedProminent).tint(jarvisCyan)
                if agent.stoppedByUser || agent.failed {
                    Button("Resume") { agent.resume() }.disabled(agent.isRunning || agent.pendingApproval != nil)
                }
                Spacer()
            }
        }
        .padding(20).frame(minWidth: 560, minHeight: 560)
    }
}
