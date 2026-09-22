#include "commandrunner.h"

#include <QQmlExtensionPlugin>
#include <qqml.h>

class CodexBarProcessPlugin : public QQmlExtensionPlugin
{
    Q_OBJECT
    Q_PLUGIN_METADATA(IID QQmlExtensionInterface_iid)

public:
    void registerTypes(const char *uri) override
    {
        qmlRegisterType<CommandRunner>(uri, 1, 0, "CommandRunner");
    }
};

#include "plugin.moc"
