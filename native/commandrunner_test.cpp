#include "commandrunner.h"

#include <QCoreApplication>
#include <QEventLoop>
#include <QFile>
#include <QPointer>
#include <QTemporaryDir>
#include <QTimer>

#include <cerrno>
#include <csignal>
#include <functional>
#include <sys/resource.h>

static void require(bool condition, const char *message)
{
    if (!condition)
        qFatal("%s", message);
}

static void waitUntil(const std::function<bool()> &done)
{
    QEventLoop loop;
    QTimer poll;
    QTimer timeout;
    timeout.setSingleShot(true);
    QObject::connect(&poll, &QTimer::timeout, &loop, [&] {
        if (done())
            loop.quit();
    });
    QObject::connect(&timeout, &QTimer::timeout, &loop, &QEventLoop::quit);
    poll.start(5);
    timeout.start(5000);
    loop.exec();
    require(done(), "Timed out waiting for process lifecycle transition");
}

int main(int argc, char **argv)
{
    QCoreApplication app(argc, argv);

    // A finished handler can submit work and later delete its own runner.
    auto *runner = new CommandRunner;
    QPointer<CommandRunner> owner(runner);
    int completions = 0;
    QObject::connect(runner, &CommandRunner::finished, &app,
                     [&](const QString &command, int exitCode,
                         const QString &out, const QString &err) {
        require(exitCode == 0 && err.isEmpty(), "Reentrant command failed");
        ++completions;
        if (completions == 1) {
            require(command == "printf first" && out == "first", "First result corrupted");
            runner->run("printf second");
        } else {
            require(completions == 2 && command == "printf second" && out == "second",
                    "Reentrant command delivered a duplicate or corrupted result");
            delete runner;
        }
    });
    runner->run("printf first");
    waitUntil([&] { return owner.isNull(); });
    require(completions == 2, "Runner destruction lost a completion");

    // Exhaust only this test process's descriptor allowance during start().
    // /bin/sh cannot start because QProcess cannot create its pipes.
    CommandRunner failed;
    int failures = 0;
    QObject::connect(&failed, &CommandRunner::finished, &app,
                     [&](const QString &command, int exitCode,
                         const QString &out, const QString &err) {
        require(command == "printf unreachable" && exitCode != 0
                    && out.isEmpty() && !err.isEmpty(),
                "Failed startup did not produce an error result");
        ++failures;
    });
    rlimit original;
    require(getrlimit(RLIMIT_NOFILE, &original) == 0, "Cannot read descriptor allowance");
    rlimit exhausted = original;
    exhausted.rlim_cur = 0;
    require(setrlimit(RLIMIT_NOFILE, &exhausted) == 0, "Cannot restrict descriptor allowance");
    failed.run("printf unreachable");
    require(setrlimit(RLIMIT_NOFILE, &original) == 0, "Cannot restore descriptor allowance");
    waitUntil([&] { return failures != 0; });

    // Destroying an owner must stop its direct child, not leave it running.
    QTemporaryDir directory;
    require(directory.isValid(), "Cannot create PID directory");
    const QString pidPath = directory.filePath("pid");
    QString quotedPath = pidPath;
    quotedPath.replace('\'', "'\\''");
    auto *active = new CommandRunner;
    bool cancelledDelivered = false;
    QObject::connect(active, &CommandRunner::finished, &app,
                     [&] { cancelledDelivered = true; });
    active->run("printf '%s' $$ > '" + quotedPath + "'; exec sleep 60");
    qint64 pid = 0;
    waitUntil([&] {
        QFile file(pidPath);
        if (file.open(QIODevice::ReadOnly))
            pid = file.readAll().toLongLong();
        return pid > 0;
    });
    delete active;
    require(kill(static_cast<pid_t>(pid), 0) == -1 && errno == ESRCH,
            "Destroying the runner left its child running");
    QCoreApplication::sendPostedEvents(nullptr, QEvent::DeferredDelete);
    QCoreApplication::processEvents();
    require(!cancelledDelivered, "Destroyed runner delivered a completion");
    require(failures == 1, "Failed startup delivered more than once");
    qInfo("CommandRunner lifecycle checks passed");
}
