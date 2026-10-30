import QtQuick 2.0
import Qing.Controls 1.0
import Qing.Frames 1.0

QPage {
    id: control
    title: "Restore vendor software?"
    property bool restoring: false
    property string message: "This removes the privacy modification and erases settings, passwords, and reading history.\n\nWi-Fi will be off. Keep the power connected."
    initialItem: QMsgPage {
        objectName: "resetConfirmation"
        title: control.title
        onExited: if (!control.restoring) control.exited()
        text: control.message
        leftButtonVisible: !control.restoring
        rightButtonVisible: !control.restoring
        leftButtonText: "Cancel"
        rightButtonText: "Restore"
        content: Item {
            anchors.fill: parent
            Column {
                objectName: "resetMessageLayout"
                anchors.centerIn: parent
                width: parent.width - 64
                spacing: 20
                QText {
                    objectName: "resetTitle"
                    width: parent.width
                    font.pixelSize: 32
                    color: "black"
                    text: control.title
                    textFormat: Text.PlainText
                    wrapMode: Text.Wrap
                    horizontalAlignment: Text.AlignHCenter
                }
                QText {
                    objectName: "resetMessage"
                    width: parent.width
                    font.pixelSize: 26
                    color: "black"
                    opacity: 0.6
                    text: control.message
                    textFormat: Text.PlainText
                    wrapMode: Text.Wrap
                    horizontalAlignment: Text.AlignHCenter
                }
            }
        }
        onLeftButtonClicked: control.exited()
        onRightButtonClicked: {
            control.restoring = true;
            control.message = "Checking the stock copy…\nKeep the power connected.";
            developerController.runCommand("/usr/local/sbin/cgs2-reset start");
        }
    }
    Timer {
        interval: 1000; repeat: true; running: control.restoring
        onTriggered: {
            var request = new XMLHttpRequest();
            request.onreadystatechange = function() {
                if (request.readyState === XMLHttpRequest.DONE && request.responseText) {
                    try {
                        var result = JSON.parse(request.responseText);
                        control.message = result.message;
                        if (result.ok === false) control.restoring = false;
                    } catch (e) {}
                }
            };
            request.open("GET", "file:///tmp/cgs2-reset-status.json?" + Date.now());
            request.send();
        }
    }
}
