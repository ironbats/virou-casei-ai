#define _GNU_SOURCE
#include <sched.h>
#include <sys/syscall.h>
#include <sys/resource.h>
#include <sys/wait.h>
#include <linux/capability.h>
#include <pthread.h>
#include <unistd.h>
#include <grp.h>
#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <signal.h>
#include <sys/prctl.h>

static void *hold(void *arg) { pause(); return arg; }
int main(int argc, char **argv) {
    if (argc != 2) return 64;
    int old = strcmp(argv[1], "before") == 0;
    if (geteuid() == 0 && (setgroups(0, NULL) || setgid(61032) || setuid(61032))) { perror("setuid"); return 77; }
    if (prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0)) return 77;
    struct rlimit limit = {64, 64};
    pthread_t tids[66];
    for (int i = 0; i < 66; ++i) {
        int rc = pthread_create(&tids[i], NULL, hold, NULL);
        if (rc) { fprintf(stderr, "pthread_create:%s\n", strerror(rc)); return 77; }
    }
    if (old && setrlimit(RLIMIT_NPROC, &limit)) return 78;
    long child = syscall(SYS_clone, CLONE_NEWUSER | SIGCHLD, NULL, NULL, NULL, 0);
    if (child == -1) { printf("{\"order\":\"%s\",\"clone_errno\":%d,\"message\":\"%s\"}\n", argv[1], errno, strerror(errno)); return 0; }
    if (child == 0) {
        if (prctl(PR_SET_PDEATHSIG, SIGKILL)) _exit(79);
        if (!old && setrlimit(RLIMIT_NPROC, &limit)) _exit(78);
        struct __user_cap_header_struct header = {_LINUX_CAPABILITY_VERSION_3, 0};
        struct __user_cap_data_struct caps[2] = {{0}, {0}};
        if (syscall(SYS_capset, &header, caps)) _exit(79);
        pid_t descendants[70];
        int count = 0, stopped_errno = 0;
        while (count < 70) {
            pid_t descendant = fork();
            if (descendant < 0) { stopped_errno = errno; break; }
            if (descendant == 0) {
                if (prctl(PR_SET_PDEATHSIG, SIGKILL)) _exit(79);
                pause();
                _exit(0);
            }
            descendants[count++] = descendant;
        }
        for (int i = 0; i < count; ++i) kill(descendants[i], SIGKILL);
        for (int i = 0; i < count; ++i) waitpid(descendants[i], NULL, 0);
        _exit(count > 0 && count < 64 && stopped_errno == EAGAIN ? 0 : 80);
    }
    int status;
    waitpid(child, &status, 0);
    printf("{\"order\":\"%s\",\"clone_errno\":0,\"child_exit\":%d}\n", argv[1], WIFEXITED(status) ? WEXITSTATUS(status) : 82);
    return 0;
}
