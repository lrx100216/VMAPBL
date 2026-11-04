/*
 * VMAPBL Butler 原生启动器。
 *
 * 应用包的主可执行文件必须是 Mach-O，macOS 才会把“本地网络”等隐私权限
 * 归到应用包本身。如果 CFBundleExecutable 只是一个 shell 脚本，
 * 子进程访问局域网会被系统静默拒绝（EHOSTUNREACH）。
 *
 * 本程序负责找到同目录下的 launcher 脚本并 fork/exec zsh，
 * 同时把终止信号转发给子进程，保证退出应用时服务能正常收尾。
 */
#include <errno.h>
#include <libgen.h>
#include <limits.h>
#include <mach-o/dyld.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/wait.h>
#include <unistd.h>

static pid_t child_pid = -1;

static void forward_signal(int sig) {
    if (child_pid > 0) {
        kill(child_pid, sig);
    }
}

int main(int argc, char *argv[]) {
    char exe[PATH_MAX];
    uint32_t size = (uint32_t)sizeof(exe);
    if (_NSGetExecutablePath(exe, &size) != 0) {
        fputs("VMAPBL: executable path too long\n", stderr);
        return 1;
    }

    char resolved[PATH_MAX];
    if (realpath(exe, resolved) == NULL) {
        fputs("VMAPBL: cannot resolve executable path\n", stderr);
        return 1;
    }

    char macos_dir[PATH_MAX];
    strlcpy(macos_dir, resolved, sizeof(macos_dir));
    char script[PATH_MAX];
    int written = snprintf(script, sizeof(script), "%s/launcher",
                           dirname(macos_dir));
    if (written < 0 || written >= (int)sizeof(script)) {
        fputs("VMAPBL: launcher path too long\n", stderr);
        return 1;
    }
    if (access(script, R_OK) != 0) {
        fprintf(stderr, "VMAPBL: launcher script not found: %s\n", script);
        return 2;
    }
    if (argc > 1 && strcmp(argv[1], "--check") == 0) {
        return 0;
    }

    signal(SIGTERM, forward_signal);
    signal(SIGINT, forward_signal);
    signal(SIGHUP, forward_signal);

    child_pid = fork();
    if (child_pid < 0) {
        perror("VMAPBL: fork");
        return 1;
    }
    if (child_pid == 0) {
        execl("/bin/zsh", "zsh", script, (char *)NULL);
        perror("VMAPBL: exec zsh");
        _exit(127);
    }

    int status = 0;
    while (waitpid(child_pid, &status, 0) < 0) {
        if (errno == EINTR) {
            continue;
        }
        perror("VMAPBL: waitpid");
        return 1;
    }
    if (WIFEXITED(status)) {
        return WEXITSTATUS(status);
    }
    if (WIFSIGNALED(status)) {
        return 128 + WTERMSIG(status);
    }
    return 1;
}
