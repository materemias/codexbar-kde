#pragma once

#include <QObject>
#include <QString>

class CommandRunner : public QObject
{
    Q_OBJECT

public:
    explicit CommandRunner(QObject *parent = nullptr) : QObject(parent) {}

    Q_INVOKABLE void run(const QString &command);

signals:
    void finished(const QString &command, int exitCode,
                  const QString &standardOutput, const QString &standardError);
};
