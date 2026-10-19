// AXPilot launcher. Read launcher/README.md before changing anything in this directory.
//
// AXPilot.app gives macOS one stable app identity to attach privacy (TCC) grants to. LaunchServices
// starts it; it reads runtime.json and posix_spawn()s the Python host as a child (never exec), so
// macOS keeps treating AXPilot.app as the host's "responsible process". It forwards SIGTERM/SIGINT/
// SIGHUP and restarts a crashed host with backoff. FROZEN: users' grants are pinned to the ad-hoc
// cdhash of this binary and Info.plist, so every change forces every user to grant access again.

import Darwin
import Foundation

let launcherVersion = "1.0.0"  // must equal CFBundleShortVersionString in Info.plist (build.sh checks)
let manifestVersion = 1  // the runtime.json schema this launcher reads
let maxRestarts = 5  // restarts allowed per window; one more failure and the launcher gives up
let restartWindow: TimeInterval = 60
let killGrace: TimeInterval = 10  // SIGKILL a host still running this long after SIGTERM/SIGINT
let exitConfig: Int32 = 78  // EX_CONFIG: bad runtime.json, or the host asks not to be restarted

struct LauncherError: Error, CustomStringConvertible { let description: String }

/// Appends timestamped lines to launcher.log (stderr if it cannot be opened). The host's stdout
/// and stderr go to the same file, so a Python traceback lands next to our lines.
final class LauncherLog: @unchecked Sendable {  // immutable after init; each line is one O_APPEND write
    let hostOutputPath: String  // launcher.log, or /dev/null if it cannot be opened
    private let fd: Int32
    private let formatter = ISO8601DateFormatter()

    init(directory: String) {
        let file = directory + "/launcher.log"
        try? FileManager.default.createDirectory(atPath: directory, withIntermediateDirectories: true)
        let size = (try? FileManager.default.attributesOfItem(atPath: file)[.size] as? UInt64) ?? 0
        if size > 5_000_000 { rename(file, file + ".1") }  // keep one old generation
        let opened = open(file, O_WRONLY | O_CREAT | O_APPEND | O_CLOEXEC, 0o644)
        hostOutputPath = opened >= 0 ? file : "/dev/null"
        fd = opened >= 0 ? opened : STDERR_FILENO
    }

    func callAsFunction(_ message: String) {
        let bytes = Array("\(formatter.string(from: Date())) launcher[\(getpid())] \(message)\n".utf8)
        _ = bytes.withUnsafeBytes { write(fd, $0.baseAddress, $0.count) }
    }
}

/// `path` with `~` expanded and, if relative, resolved against the working directory.
func absolutePath(_ path: String) -> String {
    let expanded = (path as NSString).expandingTildeInPath
    return expanded.hasPrefix("/") ? expanded : FileManager.default.currentDirectoryPath + "/" + expanded
}

/// runtime.json, written by `axpilot setup`: which interpreter to spawn, and how.
struct RuntimeManifest: Decodable {
    let version: Int
    let python: String
    let args: [String]
    let env: [String: String]?
    let cwd: String?

    static func load(from path: String) throws -> RuntimeManifest {
        func require(_ valid: Bool, _ problem: String) throws {
            if !valid { throw LauncherError(description: "\(path): \(problem)") }
        }
        let manifest = try JSONDecoder().decode(Self.self, from: Data(contentsOf: URL(fileURLWithPath: path)))
        try require(manifest.version == manifestVersion, "version \(manifest.version) is not \(manifestVersion)")
        try require(manifest.python.hasPrefix("/") && access(manifest.python, X_OK) == 0,
                    "python \"\(manifest.python)\" is not an absolute path to an executable")
        try require(manifest.cwd?.hasPrefix("/") ?? true, "cwd is not an absolute path")
        try require(!(manifest.env ?? [:]).keys.contains { $0.isEmpty || $0.contains("=") }, "invalid env key")
        return manifest
    }
}

/// Starts the host with posix_spawn. Unlike exec, this keeps AXPilot.app as the TCC responsible process.
func spawnHost(_ manifest: RuntimeManifest, outputPath: String) throws -> pid_t {
    var actions: posix_spawn_file_actions_t?
    var attrs: posix_spawnattr_t?
    posix_spawn_file_actions_init(&actions)
    posix_spawnattr_init(&attrs)
    defer {
        posix_spawn_file_actions_destroy(&actions)
        posix_spawnattr_destroy(&attrs)
    }
    // stdin is /dev/null and stdout/stderr append to launcher.log; CLOEXEC_DEFAULT closes the rest.
    posix_spawn_file_actions_addopen(&actions, STDIN_FILENO, "/dev/null", O_RDONLY, 0)
    posix_spawn_file_actions_addopen(&actions, STDOUT_FILENO, outputPath, O_WRONLY | O_CREAT | O_APPEND, 0o644)
    posix_spawn_file_actions_adddup2(&actions, STDOUT_FILENO, STDERR_FILENO)
    if let cwd = manifest.cwd { posix_spawn_file_actions_addchdir_np(&actions, cwd) }
    // We ignore the forwarded signals (dispatch delivers them to us) and ignored dispositions
    // survive exec, so the host gets every handler at its default and an empty signal mask.
    var defaultSignals = sigset_t()
    var noSignals = sigset_t()
    sigfillset(&defaultSignals)
    sigdelset(&defaultSignals, SIGKILL)
    sigdelset(&defaultSignals, SIGSTOP)
    sigemptyset(&noSignals)
    posix_spawnattr_setsigdefault(&attrs, &defaultSignals)
    posix_spawnattr_setsigmask(&attrs, &noSignals)
    posix_spawnattr_setpgroup(&attrs, 0)  // own process group: a terminal's ^C reaches it only via us
    let flags = POSIX_SPAWN_SETSIGDEF | POSIX_SPAWN_SETSIGMASK | POSIX_SPAWN_SETPGROUP | POSIX_SPAWN_CLOEXEC_DEFAULT
    posix_spawnattr_setflags(&attrs, Int16(flags))

    // The host's environment is ours (from launchd), overlaid with runtime.json's env and our markers.
    var env = ProcessInfo.processInfo.environment
    env.merge(manifest.env ?? [:]) { _, fromManifest in fromManifest }
    env["AXPILOT_LAUNCHER_VERSION"] = launcherVersion
    env["AXPILOT_LAUNCHER_BUNDLE"] = Bundle.main.bundlePath
    let argv: [UnsafeMutablePointer<CChar>?] = ([manifest.python] + manifest.args).map { strdup($0) } + [nil]
    let envp: [UnsafeMutablePointer<CChar>?] = env.map { strdup("\($0.key)=\($0.value)") } + [nil]
    defer { (argv + envp).forEach { free($0) } }
    var pid: pid_t = 0
    let status = posix_spawn(&pid, manifest.python, &actions, &attrs, argv, envp)
    guard status == 0 else {
        throw LauncherError(description: "cannot start \(manifest.python): \(String(cString: strerror(status)))")
    }
    return pid
}

/// Blocks until `pid` ends and reaps it, retrying on EINTR. Returns its exit code, 128 + signal if
/// a signal killed it (decoded by hand: Swift cannot import the W* macros), or 1 if waitpid failed.
func waitForExit(_ pid: pid_t) -> Int32 {
    var status: Int32 = 0
    while waitpid(pid, &status, 0) == -1 { if errno != EINTR { return 1 } }
    let termSignal = status & 0x7f
    return termSignal == 0 ? (status >> 8) & 0xff : 128 + termSignal
}

/// Spawns, watches and restarts the host. All mutable state is confined to the main queue.
final class Supervisor: @unchecked Sendable {
    private let manifestPath: String
    private let logger: LauncherLog
    private var childPid: pid_t = 0
    private var stopping = false
    private var recentRestarts: [Date] = []

    init(manifestPath: String, logger: LauncherLog) {
        self.manifestPath = manifestPath
        self.logger = logger
    }

    /// Starts the host, re-reading runtime.json first (`axpilot update` may have changed it).
    func start() {
        guard !stopping else { exit(0) }
        do {
            let manifest = try RuntimeManifest.load(from: manifestPath)
            let pid = try spawnHost(manifest, outputPath: logger.hostOutputPath)
            childPid = pid
            logger("started host pid \(pid): \(([manifest.python] + manifest.args).joined(separator: " "))")
            Thread.detachNewThread {
                let code = waitForExit(pid)
                DispatchQueue.main.async { self.childExited(pid: pid, code: code) }
            }
        } catch {
            logger("error: \(error)")
            scheduleRestart()
        }
    }

    /// Exit code 0 ends the launcher too, and so does 78 (EX_CONFIG: the host knows a restart
    /// cannot help). Any other code, or death by a signal (>= 129), restarts the host.
    private func childExited(pid: pid_t, code: Int32) {
        childPid = 0
        logger("host pid \(pid) exited with status \(code)\(code > 128 ? " (signal \(code - 128))" : "")")
        if stopping || code == 0 || code == exitConfig { exit(code) }
        scheduleRestart()
    }

    /// Exponential backoff (1, 2, 4, 8, 16 s); one failure more than `maxRestarts` per window is fatal.
    private func scheduleRestart() {
        let now = Date()
        recentRestarts.removeAll { now.timeIntervalSince($0) >= restartWindow }
        guard recentRestarts.count < maxRestarts else {
            logger("error: the host failed \(maxRestarts + 1) times within \(Int(restartWindow)) s; giving up")
            exit(1)
        }
        let delay = TimeInterval(1 << recentRestarts.count)
        recentRestarts.append(now)
        logger("restarting the host in \(Int(delay)) s (restart \(recentRestarts.count) of \(maxRestarts))")
        DispatchQueue.main.asyncAfter(deadline: .now() + delay) { self.start() }
    }

    /// SIGTERM and SIGINT stop the host, then us. SIGHUP is only forwarded: a host that dies of
    /// it is restarted like after any other failure. Called on the main queue.
    func handle(signal sig: Int32) {
        let name = String(cString: strsignal(sig))
        guard childPid > 0 else {
            logger("received \(name) while the host is not running")
            if sig != SIGHUP { exit(0) }
            return
        }
        logger("forwarding \(name) to host pid \(childPid)")
        kill(childPid, sig)
        guard sig != SIGHUP, !stopping else { return }
        stopping = true
        let pid = childPid
        DispatchQueue.main.asyncAfter(deadline: .now() + killGrace) {
            guard self.childPid == pid else { return }
            self.logger("host pid \(pid) still running \(Int(killGrace)) s after \(name); sending SIGKILL")
            kill(pid, SIGKILL)
        }
    }
}

func main() -> Never {
    // Only --version and --runtime <path> are ours; LaunchServices may add arguments of its own.
    var customManifest: String?
    var arguments = CommandLine.arguments.dropFirst().makeIterator()
    while let argument = arguments.next() {
        if argument == "--version" {
            print("AXPilot launcher \(launcherVersion)")
            exit(0)
        } else if argument == "--runtime" {
            guard let value = arguments.next(), !value.isEmpty else {
                fputs("usage: AXPilot [--runtime <path to runtime.json>] [--version]\n", stderr)
                exit(64)  // EX_USAGE
            }
            customManifest = absolutePath(value)
        }
    }
    let env = ProcessInfo.processInfo.environment
    let home = NSHomeDirectory()
    let appSupport = env["AXPILOT_HOME"].flatMap { $0.isEmpty ? nil : absolutePath($0) }
        ?? home + "/Library/Application Support/AXPilot"
    let logDir = env["AXPILOT_LOG_DIR"].flatMap { $0.isEmpty ? nil : absolutePath($0) }
        ?? home + "/Library/Logs/AXPilot"
    let manifestPath = customManifest ?? appSupport + "/runtime.json"
    let logger = LauncherLog(directory: logDir)
    logger("AXPilot launcher \(launcherVersion) starting: \(Bundle.main.bundlePath), runtime \(manifestPath)")
    if getppid() != 1 { logger("warning: parent is pid \(getppid()), not launchd; TCC may blame that app") }
    do { _ = try RuntimeManifest.load(from: manifestPath) } catch {
        logger("error: \(error) (run `axpilot setup`)")
        exit(exitConfig)
    }
    signal(SIGCHLD, SIG_DFL)  // children must stay waitable even if our parent ignored SIGCHLD
    signal(SIGPIPE, SIG_IGN)  // a closed stderr must not kill us (the host gets default handlers)
    let supervisor = Supervisor(manifestPath: manifestPath, logger: logger)
    let signalSources = [SIGTERM, SIGINT, SIGHUP].map { sig -> DispatchSourceSignal in
        signal(sig, SIG_IGN)  // so that only the dispatch source sees it
        let source = DispatchSource.makeSignalSource(signal: sig, queue: .main)
        source.setEventHandler { supervisor.handle(signal: sig) }
        source.resume()
        return source
    }
    supervisor.start()
    withExtendedLifetime((supervisor, signalSources)) { dispatchMain() }
}

main()
