import QtQuick 2.0

Rectangle {
    width: 720; height: 720; color: "#101417"
    property string message: "Restoring vendor software…\nKeep the power connected."
    Text {
        anchors.centerIn: parent; width: parent.width - 80
        text: parent.message; color: "white"; font.pixelSize: 30
        wrapMode: Text.WordWrap; horizontalAlignment: Text.AlignHCenter
        textFormat: Text.PlainText
    }
    function refresh() {
        var request = new XMLHttpRequest();
        request.onreadystatechange = function() {
            if (request.readyState === XMLHttpRequest.DONE && request.responseText) {
                try { message = JSON.parse(request.responseText).message; } catch (e) {}
            }
        };
        request.open("GET", "file:///tmp/cgs2-reset-status.json?" + Date.now());
        request.send();
    }
    Timer { interval: 1000; repeat: true; running: true; onTriggered: refresh() }
    Component.onCompleted: console.log("CGS2_RESET_DISPLAY_READY")
}
