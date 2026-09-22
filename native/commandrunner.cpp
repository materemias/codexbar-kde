#include "commandrunner.h"

#include <QProcess>

void CommandRunner::run(const QString &command)
{
    auto *process = new QProcess(this);
    const auto complete = [this, process, command](int exitCode, const QString &error = {}) {
        // Disconnect before notifying consumers, which may start another command
        // or destroy the runner from their finished handler.
        process->disconnect(this);
        const QString standardOutput = QString::fromUtf8(process->readAllStandardOutput());
        QString standardError = QString::fromUtf8(process->readAllStandardError());
        if (standardError.isEmpty())
            standardError = error;
        process->setParent(nullptr);
        process->deleteLater();
        emit finished(command, exitCode, standardOutput, standardError);
    };

    connect(process, &QProcess::finished, this,
            [complete](int exitCode, QProcess::ExitStatus status) {
                complete(status == QProcess::NormalExit ? exitCode : -1);
            });
    connect(process, &QProcess::errorOccurred, this,
            [process, complete](QProcess::ProcessError error) {
                // A crash also emits QProcess::finished. Only startup failure
                // needs completion here, because it has no finished signal.
                if (error == QProcess::FailedToStart)
                    complete(-1, process->errorString());
            });
    process->start(QStringLiteral("/bin/sh"), {QStringLiteral("-c"), command});
}
