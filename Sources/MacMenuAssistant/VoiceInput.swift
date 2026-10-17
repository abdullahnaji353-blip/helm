import AVFoundation
import ApplicationServices
import CoreGraphics
import Speech

@MainActor
final class VoiceInputController {
    var onStatusChange: ((Bool, String) -> Void)?
    var onPartialTranscript: ((String) -> Void)?
    var onTranscript: ((String, Bool) -> Void)?
    var onError: ((String) -> Void)?

    private let recognizer = SFSpeechRecognizer(locale: Locale.current)
    private let audioEngine = AVAudioEngine()
    private var recognitionRequest: SFSpeechAudioBufferRecognitionRequest?
    private var recognitionTask: SFSpeechRecognitionTask?
    private var eventTap: CFMachPort?
    private var hotkeyPressed = false
    private var currentCaptureUsesHotkey = false
    private var wantsToListen = false
    private var isListening = false

    func installHotkey() {
        guard eventTap == nil else { return }
        if !CGPreflightListenEventAccess() {
            _ = CGRequestListenEventAccess()
        }
        let eventMask = CGEventMask(1 << CGEventType.flagsChanged.rawValue)
        guard let tap = CGEvent.tapCreate(
            tap: .cgSessionEventTap,
            place: .headInsertEventTap,
            options: .listenOnly,
            eventsOfInterest: eventMask,
            callback: modifierEventTapCallback,
            userInfo: Unmanaged.passUnretained(self).toOpaque()
        ) else {
            let options = [
                kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String: true
            ] as CFDictionary
            _ = AXIsProcessTrustedWithOptions(options)
            publish(
                listening: false,
                status: "Grant shortcut permissions, then retry."
            )
            return
        }

        let source = CFMachPortCreateRunLoopSource(kCFAllocatorDefault, tap, 0)
        CFRunLoopAddSource(CFRunLoopGetMain(), source, .commonModes)
        CGEvent.tapEnable(tap: tap, enable: true)
        eventTap = tap
        publish(listening: false, status: "Hold Control+Option to speak.")
    }

    func toggleCapture() {
        if isListening {
            stopListening()
        } else {
            currentCaptureUsesHotkey = false
            wantsToListen = true
            requestPermissionsAndStart()
        }
    }

    fileprivate func setShortcutPressed(_ pressed: Bool) {
        guard pressed != hotkeyPressed else { return }
        hotkeyPressed = pressed
        if pressed {
            currentCaptureUsesHotkey = true
            wantsToListen = true
            requestPermissionsAndStart()
        } else {
            wantsToListen = false
            if isListening {
                stopListening()
            }
        }
    }

    fileprivate func reenableEventTap() {
        if let eventTap {
            CGEvent.tapEnable(tap: eventTap, enable: true)
        }
    }

    private func requestPermissionsAndStart() {
        switch SFSpeechRecognizer.authorizationStatus() {
        case .authorized:
            requestMicrophoneAccess()
        case .notDetermined:
            publish(listening: false, status: "Requesting Speech Recognition permission…")
            SFSpeechRecognizer.requestAuthorization { [weak self] status in
                Task { @MainActor in
                    guard let self else { return }
                    guard self.wantsToListen else {
                        self.publish(
                            listening: false,
                            status: "Permission step ended. Hold Control+Option again to speak."
                        )
                        return
                    }
                    guard status == .authorized else {
                        self.fail("Allow Speech Recognition in System Settings to use voice input.")
                        return
                    }
                    self.requestMicrophoneAccess()
                }
            }
        case .denied, .restricted:
            fail("Allow Speech Recognition in System Settings to use voice input.")
        @unknown default:
            fail("Speech recognition is unavailable.")
        }
    }

    private func requestMicrophoneAccess() {
        guard wantsToListen else { return }
        switch AVCaptureDevice.authorizationStatus(for: .audio) {
        case .authorized:
            startListening()
        case .notDetermined:
            publish(listening: false, status: "Requesting Microphone permission…")
            AVCaptureDevice.requestAccess(for: .audio) { [weak self] granted in
                Task { @MainActor in
                    guard let self else { return }
                    guard self.wantsToListen else {
                        self.publish(
                            listening: false,
                            status: "Permission step ended. Hold Control+Option again to speak."
                        )
                        return
                    }
                    guard granted else {
                        self.fail("Allow microphone access in System Settings to use voice input.")
                        return
                    }
                    self.startListening()
                }
            }
        case .denied, .restricted:
            fail("Allow microphone access in System Settings to use voice input.")
        @unknown default:
            fail("Microphone access is unavailable.")
        }
    }

    private func startListening() {
        guard wantsToListen, !isListening else { return }
        guard let recognizer, recognizer.isAvailable else {
            fail("Speech recognition is unavailable for the current language.")
            return
        }
        guard recognizer.supportsOnDeviceRecognition else {
            fail(
                "On-device speech recognition is unavailable for the current language. "
                    + "Voice audio was not sent anywhere."
            )
            return
        }

        let request = SFSpeechAudioBufferRecognitionRequest()
        request.requiresOnDeviceRecognition = true
        request.shouldReportPartialResults = true
        recognitionRequest = request
        recognitionTask = recognizer.recognitionTask(with: request) { [weak self] result, error in
            if let result, result.isFinal {
                let transcript = result.bestTranscription.formattedString
                Task { @MainActor in
                    self?.finishRecognition(transcript)
                }
            } else if let result {
                let transcript = result.bestTranscription.formattedString
                Task { @MainActor in
                    self?.onPartialTranscript?(transcript)
                }
            } else if let error {
                Task { @MainActor in
                    self?.fail("Speech recognition failed: \(error.localizedDescription)")
                }
            }
        }

        let input = audioEngine.inputNode
        input.installTap(
            onBus: 0,
            bufferSize: 1024,
            format: input.outputFormat(forBus: 0)
        ) { buffer, _ in
            request.append(buffer)
        }
        audioEngine.prepare()
        do {
            try audioEngine.start()
            isListening = true
            publish(listening: true, status: "Listening… release Control+Option when done.")
        } catch {
            input.removeTap(onBus: 0)
            recognitionTask?.cancel()
            recognitionTask = nil
            recognitionRequest = nil
            fail("Could not start microphone capture: \(error.localizedDescription)")
        }
    }

    private func stopListening() {
        guard isListening else { return }
        wantsToListen = false
        isListening = false
        audioEngine.stop()
        audioEngine.inputNode.removeTap(onBus: 0)
        recognitionRequest?.endAudio()
        publish(listening: false, status: "Transcribing on this Mac…")
    }

    private func finishRecognition(_ transcript: String) {
        recognitionTask = nil
        recognitionRequest = nil
        guard !transcript.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
            fail("I didn't catch that. Try speaking again.")
            return
        }
        publish(listening: false, status: "Starting your voice task…")
        onTranscript?(transcript, currentCaptureUsesHotkey)
        currentCaptureUsesHotkey = false
    }

    private func fail(_ message: String) {
        wantsToListen = false
        if isListening {
            audioEngine.stop()
            audioEngine.inputNode.removeTap(onBus: 0)
        }
        isListening = false
        recognitionTask?.cancel()
        recognitionTask = nil
        recognitionRequest?.endAudio()
        recognitionRequest = nil
        publish(listening: false, status: "Voice input unavailable.")
        onError?(message)
    }

    private func publish(listening: Bool, status: String) {
        onStatusChange?(listening, status)
    }
}

private let modifierEventTapCallback: CGEventTapCallBack = {
    _, type, event, userInfo in
    guard let userInfo else {
        return Unmanaged.passUnretained(event)
    }
    let controller = Unmanaged<VoiceInputController>
        .fromOpaque(userInfo)
        .takeUnretainedValue()
    if type == .tapDisabledByTimeout || type == .tapDisabledByUserInput {
        Task { @MainActor in
            controller.reenableEventTap()
        }
        return Unmanaged.passUnretained(event)
    }
    guard type == .flagsChanged else {
        return Unmanaged.passUnretained(event)
    }
    let flags = event.flags
    let pressed = flags.contains(.maskControl) && flags.contains(.maskAlternate)
    Task { @MainActor in
        controller.setShortcutPressed(pressed)
    }
    return Unmanaged.passUnretained(event)
}
