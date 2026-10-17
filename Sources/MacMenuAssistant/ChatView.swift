import AVFoundation
import AppKit
import SwiftUI

private let jarvisCyan = Color(red: 0.30, green: 0.85, blue: 1.0)
private let jarvisBlue = Color(red: 0.16, green: 0.42, blue: 1.0)

struct ChatLine: Identifiable, Equatable {
    enum Role { case user, bot, activity, error }
    let id = UUID()
    let role: Role
    var text: String
}

/// Drives the Chatbot conversation through `assistant.py --chat`, which keeps
/// history and calls device tools (apps, URLs, volume, status, screen control).
@MainActor
final class ChatController: ObservableObject {
    @Published var lines: [ChatLine] = []
    @Published var draft = ""
    @Published var isThinking = false
    @Published var isListening = false
    @Published var voiceStatus = "Hold ⌃⌥ to talk"
    @Published var pendingApproval: String?
    @Published var askBeforeActing = true
    @Published var speakReplies = true

    private var process: Process?
    private var inputPipe: Pipe?
    private var buffer = ""
    private let voice = VoiceInputController()
    private let speech = AVSpeechSynthesizer()
    private var voiceConfigured = false

    private var projectRoot: URL {
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
        guard !voiceConfigured else { return }
        voiceConfigured = true
        voice.onStatusChange = { [weak self] listening, status in
            self?.isListening = listening
            self?.voiceStatus = listening ? "Listening…" : status
        }
        voice.onError = { [weak self] message in
            self?.lines.append(ChatLine(role: .error, text: message))
        }
        voice.onPartialTranscript = { [weak self] transcript in
            self?.draft = transcript
        }
        voice.onTranscript = { [weak self] transcript, _ in
            self?.draft = transcript
            self?.send()
        }
        voice.installHotkey()
    }

    func toggleVoice() { voice.toggleCapture() }

    func send() {
        let text = draft.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty, process == nil else { return }
        draft = ""
        lines.append(ChatLine(role: .user, text: text))
        launch(["--chat", text, "--autonomy", askBeforeActing ? "approval" : "autonomous"])
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

    private func respond(_ answer: String) {
        guard let inputPipe, pendingApproval != nil else { return }
        inputPipe.fileHandleForWriting.write(Data(answer.utf8))
        pendingApproval = nil
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
            lines.append(ChatLine(role: .error, text: "Could not start Chatbot: \(error.localizedDescription)"))
        }
    }

    private func receive(_ text: String) {
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
                    speak(reply)
                }
            } else if line.hasPrefix("TOOL: ") {
                let name = line.dropFirst("TOOL: ".count).split(separator: " ").first.map(String.init) ?? ""
                addActivity(Self.toolLabels[name] ?? "Working on it…")
            } else if line.hasPrefix("Error:") || line.hasPrefix("SCREEN_ACCESS_DENIED:") {
                lines.append(ChatLine(role: .error, text: line))
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

    private func speak(_ text: String) {
        guard speakReplies else { return }
        let utterance = AVSpeechUtterance(string: text)
        utterance.voice = AVSpeechSynthesisVoice(identifier: "com.apple.voice.compact.en-GB.Daniel")
            ?? AVSpeechSynthesisVoice(language: "en-GB")
        speech.stopSpeaking(at: .immediate)
        speech.speak(utterance)
    }
}

/// Chat by default; the original panel stays reachable for permissions and MCP.
struct ChatRootView: View {
    @State private var showAdvanced = false

    var body: some View {
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
                Text("CHATBOT")
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
                Divider()
                Button("New chat") { chat.newChat() }
                Button("Permissions & MCP tools…") { showAdvanced = true }
                Divider()
                Button("Quit Chatbot") { NSApplication.shared.terminate(nil) }
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

            TextField("Message Chatbot…", text: $chat.draft, axis: .vertical)
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
        .accessibilityLabel("Chatbot is thinking")
    }
}
