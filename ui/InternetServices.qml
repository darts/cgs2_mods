import QtQuick 2.7
import QtQuick.Layouts 1.3
import Qt.labs.settings 1.0
import Qing.Controls 1.0
import Qing.Frames 1.0
import Qing.Keyboards 1.0

QPage {
    id: control
    objectName: "internetServicesPage"
    title: "Internet services"
    property var draft: defaults()
    property var statusData: ({})
    property bool loaded: false
    property bool dirty: false
    property bool applying: false
    property bool applyUnconfirmed: false
    property string submittedId: ""
    property string submittedJson: ""
    property string statusReadError: ""
    property double lastRequestTime: 0
    property string feedback: ""
    property string searchQuery: ""
    property string searchId: ""
    property bool searching: false
    property string searchFeedback: ""
    property string searchDetails: ""
    property var searchResults: []
    property string geocoderUrl: locationStore.url || "https://geocoding-api.open-meteo.com/v1/search"
    readonly property string screen: mainStackView.currentItem ? (mainStackView.currentItem.screenName || "") : ""
    property alias searchPayload: searchStore.payload
    property alias requestPayload: requestStore.payload
    readonly property var timeFields: [
        {key: "time_servers", label: "Servers", hint: "IPv4 addresses or hostnames, separated by commas"},
        {key: "time_port", label: "UDP port", hint: "1 to 65535"},
        {key: "time_interval_hours", label: "Every (hours)", hint: "Hours between time synchronizations"},
        {key: "time_timezone", label: "Time zone", hint: "IANA zone, for example Europe/Berlin or Etc/UTC"}
    ]
    readonly property var weatherFields: [
        {key: "weather_url", label: "API URL", hint: "HTTPS weather API URL, including for an approved LAN service"},
        {key: "weather_latitude", label: "Latitude", hint: "-90 to 90; a city centre is sufficient"},
        {key: "weather_longitude", label: "Longitude", hint: "-180 to 180; a city centre is sufficient"},
        {key: "weather_location", label: "Place label", hint: "A display name for this location"},
        {key: "weather_timezone", label: "Time zone", hint: "IANA zone used for the weather forecast"},
        {key: "weather_temperature_unit", label: "Units", hint: "celsius or fahrenheit"},
        {key: "weather_interval_minutes", label: "Every (mins)", hint: "Minutes between weather requests"}
    ]
    readonly property var dnsFields: [
        {key: "dns_servers", label: "Resolvers", hint: "Numeric IPv4 addresses, separated by commas"},
        {key: "dns_port", label: "DNS port", hint: "1 to 65535"}
    ]
    Settings {
        id: requestStore
        fileName: "/data/etc/cgs2-services-draft.ini"
        category: "request"
        property string payload: ""
    }

    Settings {
        id: locationStore
        fileName: "/data/etc/cgs2-location-search.ini"
        category: "preferences"
        property string url: "https://geocoding-api.open-meteo.com/v1/search"
    }
    Settings {
        id: searchStore
        fileName: "/data/etc/cgs2-location-search.ini"
        category: "request"
        property string payload: ""
    }

    function defaults() {
        var zone = currentTimeZone() || "Etc/UTC";
        return {version: 1, request_id: "", time_enabled: false,
            time_servers: "162.159.200.1,162.159.200.123", time_port: "123",
            time_interval_hours: "6", time_timezone: zone,
            weather_enabled: false, weather_url: "https://api.open-meteo.com/v1/forecast",
            weather_latitude: "", weather_longitude: "", weather_location: "",
            weather_timezone: zone, weather_temperature_unit: "celsius",
            weather_interval_minutes: "30", dns_enabled: false,
            dns_servers: "9.9.9.9,149.112.112.112", dns_port: "53",
            allow_dns_rotation: false, allow_custom_lan: false};
    }
    function change(key, value) {
        var next = JSON.parse(JSON.stringify(draft));
        next[key] = value;
        draft = next;
        dirty = true;
    }
    function shortValue(value) {
        var text = String(value || "Not set").replace(/</g, "‹").replace(/>/g, "›");
        return text.length > 18 ? text.slice(0, 17) + "…" : text;
    }
    function readJson(path, callback) {
        var request = new XMLHttpRequest();
        request.onreadystatechange = function() {
            if (request.readyState !== XMLHttpRequest.DONE) return;
            var value;
            try { value = JSON.parse(request.responseText); }
            catch (e) { callback(null, "Saved information is not available yet."); return; }
            callback(value, "");
        };
        try { request.open("GET", path); request.send(); }
        catch (e) { callback(null, "Saved information could not be read."); }
    }
    function acceptConfig(value) {
        var next = defaults();
        if (value && value.version === 1) {
            for (var key in next) {
                if (value.hasOwnProperty(key) && typeof value[key] === typeof next[key])
                    next[key] = value[key];
            }
        }
        draft = next;
        dirty = false;
        loaded = true;
        applyUnconfirmed = false;
    }
    function loadConfig() {
        if (applying) return;
        loaded = false;
        readJson("file:///etc/cgs2-privacy/services.json", function(value, error) {
            control.acceptConfig(value);
            if (error) control.feedback = "No saved services loaded. All services start disabled.";
        });
    }
    function acceptStatus(value) {
        if (!value || typeof value !== "object") return;
        statusData = value;
        if ((applying || applyUnconfirmed) && value.last_apply_id === submittedId && typeof value.apply_ok === "boolean") {
            var draftUnchanged = JSON.stringify(draft) === submittedJson;
            var wasApplying = applying;
            applying = false;
            applyUnconfirmed = false;
            feedback = value.apply_ok ? "Saved." : "Could not save. Open Details.";
            dirty = !value.apply_ok || !draftUnchanged;
            if (value.apply_ok && !draftUnchanged) feedback = "Saved. Newer edits are not saved.";
            if (value.apply_ok) {
                var savedZone = JSON.parse(submittedJson).time_timezone;
                var index = timeZoneIndex(savedZone);
                if (index >= 0 && datetimeManager.timezoneConfig.name !== savedZone)
                    datetimeManager.timezoneConfig.timezoneIndex = index;
                if (wasApplying && draftUnchanged && screen === "approval") control.mainStackView.pop(null);
            }
        }
    }
    function expireApply() {
        if (!applying) return;
        applying = false;
        applyUnconfirmed = true;
        dirty = true;
        feedback = "The save is not confirmed. Your edits stay. Examine the details.";
    }
    function timeZoneIndex(name) {
        if (typeof datetimeManager === "undefined" || !datetimeManager.timezoneConfig) return -1;
        var zones = datetimeManager.timezoneConfig.timezoneList || [];
        for (var i = 0; i < zones.length; i++) if (zones[i].name === name) return i;
        return -1;
    }
    function currentTimeZone() {
        if (typeof datetimeManager === "undefined" || !datetimeManager.timezoneConfig) return "";
        return String(datetimeManager.timezoneConfig.name || "");
    }
    function timeZoneSupported(name) {
        // The native curated picker omits valid active zones such as
        // Europe/Berlin. Preserve that exact zone without substituting a
        // different region or using getTimezoneIndex()'s fallback index 0.
        return !!name && (name === currentTimeZone() || timeZoneIndex(name) >= 0);
    }
    function loadStatus() {
        readJson("file:///run/cgs2-services/status.json", function(value, error) {
            control.statusReadError = error ? "Service status is unavailable." : "";
            if (value) control.acceptStatus(value);
        });
    }
    function integerInRange(value, minimum, maximum) {
        return /^[0-9]+$/.test(String(value)) && Number(value) >= minimum && Number(value) <= maximum;
    }
    function coordinate(value, minimum, maximum) {
        return /^-?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)$/.test(String(value)) &&
            isFinite(Number(value)) && Number(value) >= minimum && Number(value) <= maximum;
    }
    function validate() {
        if (draft.time_enabled) {
            if (!String(draft.time_servers).trim()) return "Enter at least one time server.";
            if (!integerInRange(draft.time_port, 1, 65535)) return "Time port must be 1 to 65535.";
            if (!integerInRange(draft.time_interval_hours, 1, 8760)) return "Time interval must be 1 to 8760 hours.";
        }
        if (draft.weather_enabled) {
            if (!String(draft.weather_latitude).trim() || !String(draft.weather_longitude).trim())
                return "Choose a location in Weather.";
            if (!/^https:\/\/[^\s]+$/.test(draft.weather_url)) return "Enter an HTTPS weather URL.";
            if (!coordinate(draft.weather_latitude, -90, 90)) return "Latitude must be between -90 and 90.";
            if (!coordinate(draft.weather_longitude, -180, 180)) return "Longitude must be between -180 and 180.";
            if (draft.weather_temperature_unit !== "celsius" && draft.weather_temperature_unit !== "fahrenheit")
                return "Units must be celsius or fahrenheit.";
            if (!integerInRange(draft.weather_interval_minutes, 1, 10080)) return "Weather interval must be 1 to 10080 minutes.";
        }
        if (draft.dns_enabled) {
            if (!String(draft.dns_servers).trim()) return "Enter at least one DNS resolver IPv4 address.";
            if (!integerInRange(draft.dns_port, 1, 65535)) return "DNS port must be 1 to 65535.";
        }
        if (!draft.time_timezone || !draft.weather_timezone) return "Enter both time zones, for example Etc/UTC.";
        if (!timeZoneSupported(draft.time_timezone)) return "Keep the current time zone or select one from the Time zone list.";
        return "";
    }
    function save() {
        if (!loaded || applying) return;
        var error = validate();
        if (error) { feedback = error; return; }
        var request = JSON.parse(JSON.stringify(draft));
        lastRequestTime = Math.max(Date.now(), lastRequestTime + 1);
        request.request_id = String(lastRequestTime);
        submittedId = request.request_id;
        submittedJson = JSON.stringify(request);
        draft = request;
        try {
            var encoded = Qt.btoa(unescape(encodeURIComponent(submittedJson)));
            requestStore.payload = encoded;
            // Qt.labs.Settings delays property persistence. setValue writes the
            // current draft before sync, so the helper cannot read the old one.
            requestStore.setValue("payload", encoded);
            requestStore.sync();
        } catch (e) {
            feedback = "The settings could not be prepared. Nothing was sent.";
            return;
        }
        applyUnconfirmed = false;
        applying = true;
        feedback = "Saving…";
        try {
            developerController.runCommand("/usr/local/sbin/cgs2-services apply");
        } catch (e) {
            expireApply();
        }
    }
    function syncNow() {
        if (!loaded || applying || dirty || !draft.time_enabled) {
            feedback = "First, approve and save the enabled time settings."; return;
        }
        feedback = "Time sync requested.";
        developerController.runCommand("/usr/local/sbin/cgs2-services time-now");
    }
    function updateWeather() {
        if (!loaded || applying || dirty || !draft.weather_enabled) {
            feedback = "First, approve and save the enabled weather settings."; return;
        }
        feedback = "Weather update requested.";
        developerController.runCommand("/usr/local/sbin/cgs2-services weather-now");
    }
    function requestedAccess() {
        var lines = [];
        if (draft.time_enabled) lines.push("Time: " + draft.time_servers + " · UDP " + draft.time_port);
        if (draft.weather_enabled) lines.push("Weather: " + draft.weather_url);
        if (draft.dns_enabled) lines.push("DNS: " + draft.dns_servers + " · UDP/TCP " + draft.dns_port);
        if (!lines.length) lines.push("No time, weather or DNS server access requested.");
        lines.push(draft.allow_dns_rotation ? "DNS rotation is approved. The device can update the resolved server addresses automatically." : "DNS rotation is not approved.");
        if (draft.allow_custom_lan) lines.push("Private LAN addresses are allowed for the chosen time and weather servers.");
        return lines.join("\n");
    }
    function approvedServers() {
        var servers = statusData.approved_servers;
        if (!Array.isArray(servers)) return "Connection permission information is not available yet.";
        return servers.length ? servers.join("\n") : "No active connection permissions in the last report.";
    }
    function serviceStatus(name) {
        var service = statusData[name] || {};
        var text = service.message || "No result recorded.";
        if (service.last_success) text += "\nLast success: " + service.last_success;
        return text;
    }
    function reading(value, suffix) {
        return typeof value === "number" && isFinite(value) ? String(Math.round(value * 10) / 10) + suffix : "Unavailable";
    }
    function weatherCondition(code) {
        if (code === 0) return "Clear";
        if (code === 1) return "Mainly clear";
        if (code === 2) return "Partly cloudy";
        if (code === 3) return "Overcast";
        if (code === 45 || code === 48) return "Fog";
        if (code >= 51 && code <= 57) return "Drizzle";
        if ((code >= 61 && code <= 67) || (code >= 80 && code <= 82)) return "Rain";
        if ((code >= 71 && code <= 77) || code === 85 || code === 86) return "Snow";
        if (code === 95 || code === 96 || code === 99) return "Thunderstorm";
        return "Condition unavailable";
    }
    function weatherReadings() {
        var snapshot = (statusData.weather || {}).snapshot;
        if (!snapshot || !snapshot.current) return "No weather downloaded yet.";
        var current = snapshot.current;
        var unit = snapshot.temperature_unit || "";
        var place = snapshot.location || (String(snapshot.latitude) + ", " + String(snapshot.longitude));
        var lines = ["Outdoor model estimate · " + place,
            weatherCondition(current.weather_code),
            "Temperature: " + reading(current.temperature, " " + unit),
            "Humidity: " + reading(current.humidity, "%"),
            "Wind: " + reading(current.wind_speed, " " + (snapshot.wind_speed_unit || ""))];
        if (current.time) lines.push("Weather time: " + current.time);
        if (snapshot.updated_at) lines.push("Downloaded: " + snapshot.updated_at);
        var days = snapshot.daily || [];
        if (days.length) lines.push("Daily forecast:");
        for (var i = 0; i < Math.min(days.length, 7); i++) {
            var day = days[i];
            lines.push(day.date + " · " + weatherCondition(day.weather_code) + "\n" +
                reading(day.minimum_temperature, unit) + " to " + reading(day.maximum_temperature, unit) +
                " · Rain " + reading(day.precipitation_probability, "%"));
        }
        return lines.join("\n");
    }
    function navigate(name) {
        var pages = {time: timePage, weather: weatherPage, advanced: advancedPage,
            approval: approvalPage, details: detailsPage, search: searchPage,
            searchPermission: searchPermissionPage, searchResults: searchResultsPage,
            timeProvider: timeProviders, weatherProvider: weatherProviders, zones: timeZones};
        if (pages[name]) control.mainStackView.push(pages[name]);
    }
    function editField(key, label, hint) {
        if (key === "time_timezone") navigate("zones");
        else control.mainStackView.push(editor, {fieldKey:key, label:label, hint:hint || ""});
    }
    function reviewSave() {
        if (!loaded || applying) return;
        var error = validate();
        if (error) { feedback = error; return; }
        feedback = "";
        navigate("approval");
    }
    function zoneLabel(zone) {
        if (zone === "Etc/UTC" || zone === "UTC") return "UTC";
        var parts = String(zone).split("/");
        return parts[parts.length - 1].replace(/_/g, " ");
    }
    function timeProviderName() {
        return draft.time_servers === "162.159.200.1,162.159.200.123" && draft.time_port === "123" ? "Cloudflare" : "Custom";
    }
    function weatherProviderName() {
        return draft.weather_url === "https://api.open-meteo.com/v1/forecast" ? "Open-Meteo" : "Custom";
    }
    function selectTimeProvider(name) {
        if (name === "Cloudflare") {
            change("time_servers", "162.159.200.1,162.159.200.123");
            change("time_port", "123");
        }
    }
    function stageWeatherDns() {
        if (draft.weather_enabled && !/^https:\/\/\d+\.\d+\.\d+\.\d+(?=[:/]|$)/.test(draft.weather_url)) {
            change("dns_enabled", true);
            change("allow_dns_rotation", true);
        }
    }
    function setWeatherEnabled(value) {
        change("weather_enabled", value);
        if (value) stageWeatherDns();
    }
    function selectWeatherProvider(name) {
        if (name === "Open-Meteo") {
            change("weather_url", "https://api.open-meteo.com/v1/forecast");
            stageWeatherDns();
        }
    }
    function serviceSummary(name) {
        if (!draft[name + "_enabled"]) return "Off";
        if (name === "weather" && !draft.weather_location && !draft.weather_latitude) return "Choose a location";
        if (dirty) return "Not saved";
        var service = statusData[name] || {};
        if (name === "time" && (service.message || "").indexOf("use Sync now to authorize this correction") >= 0) return "Tap Sync now";
        if (name === "weather" && /certificate.*(expired|not yet valid)|current time.*(before|after)/i.test(service.message || "")) return "Sync time, then retry";
        if (/network is unreachable|no route to host|network unavailable/i.test(service.message || "")) return "Enable Wi-Fi in Settings";
        if (service.message && /failed|error|timed out/i.test(service.message)) return "Check Details";
        if (!service.last_success) return "On";
        var minutes = Math.floor((Date.now() - Date.parse(service.last_success)) / 60000);
        if (!isFinite(minutes) || minutes < 0) return "Updated";
        if (minutes < 1) return "Just updated";
        if (minutes < 60) return "Updated " + minutes + "m ago";
        if (minutes < 1440) return "Updated " + Math.floor(minutes / 60) + "h ago";
        return "Updated " + Math.floor(minutes / 1440) + "d ago";
    }
    function endpointHost(url) {
        var match = /^https:\/\/([^/:?#]+)/.exec(url);
        return match ? match[1] : url;
    }
    function dnsProviderLabel() {
        var servers = String(draft.dns_servers).replace(/\s/g, "");
        if (servers === "9.9.9.9,149.112.112.112") return "Quad9";
        if (servers === "1.1.1.1,1.0.0.1") return "Cloudflare";
        return servers;
    }
    function approvalSummary() {
        var lines = [];
        if (draft.time_enabled) lines.push("Time · " + (timeProviderName() === "Cloudflare" ? "Cloudflare" : draft.time_servers));
        if (draft.weather_enabled) lines.push("Weather · " + (weatherProviderName() === "Open-Meteo" ? "Open-Meteo" : endpointHost(draft.weather_url)));
        if (draft.dns_enabled) lines.push("DNS · " + dnsProviderLabel());
        if (!lines.length) lines.push("Time, weather and DNS stay blocked.");
        return lines.join("\n\n");
    }
    function connectionDetails(search) {
        var body;
        if (search) {
            body = "Lookup: " + geocoderUrl;
            body += searchNeedsDns() ? "\n\nDNS: " + draft.dns_servers + " · UDP/TCP " + draft.dns_port +
                "\nDNS may follow updated addresses for this lookup only." : "\n\nNumeric endpoint: no DNS lookup.";
            if (draft.allow_custom_lan) body += "\n\nPrivate LAN addresses are allowed for this lookup.";
            body += "\n\nThe provider receives your query and IP. Saved service permissions stay unchanged.";
            if (searchDetails) body += "\n\nLast lookup: " + searchDetails;
        } else {
            body = requestedAccess();
            if (statusData.last_apply_id === submittedId && statusData.apply_ok === false && statusData.message)
                body += "\n\nSave result: " + statusData.message;
            if (statusReadError) body += "\n\n" + statusReadError;
            if (draft.weather_enabled) body += "\n\nWeather receives your IP and chosen coordinates.";
        }
        control.mainStackView.push(connectionsPage, {body:body});
    }
    function setGeocoder(value) {
        geocoderUrl = String(value).trim();
        locationStore.url = geocoderUrl;
        locationStore.setValue("url", geocoderUrl);
        locationStore.sync();
    }
    function locationLabel(result) {
        var parts = [result.name];
        if (result.admin1 && result.admin1 !== result.name) parts.push(result.admin1);
        if (result.country) parts.push(result.country);
        return parts.join(", ");
    }
    function selectLocation(result) {
        change("weather_latitude", String(result.latitude));
        change("weather_longitude", String(result.longitude));
        change("weather_location", locationLabel(result));
        change("weather_timezone", result.timezone);
        control.mainStackView.pop(null);
        navigate("weather");
    }
    function searchHost() {
        var match = /^https:\/\/([^/:?#]+)(?::[0-9]+)?(?:[/?#]|$)/.exec(geocoderUrl);
        return match ? match[1] : "Invalid HTTPS address";
    }
    function searchNeedsDns() {
        return !/^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$/.test(searchHost());
    }
    function requestSearch(query) {
        searchQuery = String(query).trim();
        searchFeedback = "";
        searchDetails = "";
        if (searchQuery.length < 2) { searchFeedback = "Enter a city or postcode."; control.showError(searchFeedback); return; }
        if (!/^https:\/\/[^\s]+$/.test(geocoderUrl)) { searchFeedback = "Set an HTTPS lookup URL in Advanced."; control.showError(searchFeedback); return; }
        navigate("searchPermission");
    }
    function searchNow() {
        if (searching || !searchQuery) return;
        lastRequestTime = Math.max(Date.now(), lastRequestTime + 1);
        searchId = String(lastRequestTime);
        var request = {request_id:searchId, query:searchQuery, language:"en", endpoint:geocoderUrl,
            dns_servers:draft.dns_servers, dns_port:draft.dns_port,
            allow_custom_lan:draft.allow_custom_lan, allow_dns_rotation:true, approved:true};
        try {
            var encoded = Qt.btoa(unescape(encodeURIComponent(JSON.stringify(request))));
            searchStore.payload = encoded;
            searchStore.setValue("payload", encoded);
            searchStore.sync();
            searching = true;
            searchResults = [];
            searchFeedback = "Searching…";
            developerController.runCommand("/usr/local/sbin/cgs2-services geocode");
        } catch (e) { searching = false; searchFeedback = "Search could not start. Try again."; }
        control.mainStackView.pop();
        navigate("searchResults");
    }
    function acceptSearch(value) {
        if (!value || value.request_id !== searchId || value.busy === true || typeof value.ok !== "boolean") return;
        searching = false;
        searchResults = value.ok && Array.isArray(value.results) ? value.results : [];
        searchDetails = String(value.message || "");
        if (value.ok) searchFeedback = searchResults.length ? "" : "No matches. Try a nearby city.";
        else if (/network is unreachable|no route to host|network unavailable/i.test(searchDetails)) searchFeedback = "Enable Wi-Fi in Settings.";
        else if (/certificate.*(expired|not yet valid)|current time.*(before|after)/i.test(searchDetails)) searchFeedback = "Sync time, then retry.";
        else searchFeedback = "Search failed. Open Details.";
    }
    function loadSearch() {
        if (!searching) return;
        readJson("file:///run/cgs2-services/location-search.json", function(value, error) {
            if (value) control.acceptSearch(value);
        });
    }
    function expireSearch() {
        searching = false;
        searchFeedback = "Search timed out. Try again.";
    }
    Component.onCompleted: {
        developerController.runCommand("/usr/local/sbin/cgs2-services start");
        loadConfig();
        loadStatus();
    }
    Timer { interval: 2000; repeat: true; running: control.visible; onTriggered: control.loadStatus() }
    Timer { interval: 1000; repeat: true; running: control.searching; onTriggered: control.loadSearch() }
    // Elapsed timers remain bounded when the clock changes.
    Timer { interval: 45000; repeat: false; running: control.applying; onTriggered: control.expireApply() }
    Timer { interval: 45000; repeat: false; running: control.searching; onTriggered: control.expireSearch() }

    initialItem: QChildPage {
        property string screenName: "overview"
        title: control.title; onExited: control.exited()
        Column {
            anchors.left: parent.left; anchors.right: parent.right; anchors.top: parent.top
            anchors.margins: 30; anchors.topMargin: 6; spacing: 16
            Repeater {
                model: ["Time", "Weather", "Advanced"]
                delegate: Rectangle {
                    objectName: "servicesNavigate_" + modelData.toLowerCase()
                    signal clicked()
                    width: parent.width; height: 112; radius: 24; color: "#1AFFFFFF"
                    enabled: control.loaded && !control.applying
                    Column { anchors.left: parent.left; anchors.leftMargin: 28; anchors.verticalCenter: parent.verticalCenter; spacing: 6
                        QText { text: modelData; font.pixelSize: 34 }
                        QText { text: index === 2 ? "Servers, permissions and details" : control.serviceSummary(modelData.toLowerCase()); font.pixelSize: 23; opacity: 0.55 }
                    }
                    QText { anchors.right: parent.right; anchors.rightMargin: 28; anchors.verticalCenter: parent.verticalCenter; text: "›"; font.pixelSize: 42; opacity: 0.6 }
                    MouseArea { anchors.fill: parent; onClicked: parent.clicked() }
                    onClicked: control.navigate(modelData.toLowerCase())
                }
            }
        }
    }
    Rectangle {
        objectName: "servicesFooter"
        anchors.left: parent.left; anchors.right: parent.right; anchors.bottom: parent.bottom
        height: 140; color: "#111111"; z: 20
        visible: ["overview", "time", "weather", "advanced", "details", "approval"].indexOf(control.screen) >= 0
        QText {
            objectName: "servicesFeedback"; anchors.left: parent.left; anchors.right: parent.right
            anchors.top: parent.top; anchors.margins: 30; anchors.topMargin: 5
            font.pixelSize: 22; textFormat: Text.PlainText; text: control.feedback || (control.dirty ? "Unsaved changes" : "")
        }
        QButton {
            objectName: "servicesSave"; anchors.left: parent.left; anchors.right: parent.right
            anchors.bottom: parent.bottom; anchors.margins: 30; anchors.bottomMargin: 18
            height: 80; radius: 24; backgroundColor: "#315C72"
            text: control.applying ? "Saving…" : control.screen === "approval" ? "Approve & save" : "Save"
            enabled: control.loaded && !control.applying && !control.searching && (control.dirty || control.screen === "approval")
            onClicked: { if (control.screen === "approval") control.save(); else control.reviewSave(); }
        }
    }
    Component { id: timePage
        QChildPage { property string screenName: "time"; title: "Time"; onExited: control.mainStackView.pop()
            QFlickable { objectName: "servicesForm_time"; anchors.fill: parent; anchors.bottomMargin: 140
                contentHeight: contentColumn.height
                ColumnLayout { id: contentColumn; x: 30; width: parent.width - 60; spacing: 12
                    enabled: control.loaded && !control.applying
                    QSwitchItem { objectName: "servicesSwitch_time_enabled"; Layout.fillWidth: true
                    text: "Automatic time"; checked: control.draft.time_enabled
                    onCheckedChanged: if (control.loaded && control.draft.time_enabled !== checked) control.change("time_enabled", checked) }
QOptionItem { objectName: "servicesTimeProvider"; Layout.fillWidth: true
                    name: "Provider"; childText: control.timeProviderName(); onClicked: control.navigate("timeProvider") }
QOptionItem { objectName: "servicesTimeZone"; Layout.fillWidth: true
                    name: "Time zone"; childText: control.zoneLabel(control.draft.time_timezone); onClicked: control.navigate("zones") }
QButton { objectName: "servicesTimeNow"; Layout.fillWidth: true; radius: 22; text: "Sync now"
                    enabled: !control.dirty && control.draft.time_enabled; onClicked: control.syncNow() }
QTextArea { objectName: ""; Layout.fillWidth: true; Layout.preferredHeight: contentHeight
                    font.pixelSize: 23; textFormat: Text.PlainText; opacity: 0.7; text: "Sync now also approves clock corrections of more than five minutes." }
                    Item { Layout.preferredHeight: 12 }
                }
            }
        }
    }
    Component { id: weatherPage
        QChildPage { property string screenName: "weather"; title: "Weather"; onExited: control.mainStackView.pop()
            QFlickable { objectName: "servicesForm_weather"; anchors.fill: parent; anchors.bottomMargin: 140
                contentHeight: contentColumn.height
                ColumnLayout { id: contentColumn; x: 30; width: parent.width - 60; spacing: 8
                    enabled: control.loaded && !control.applying
                    QSwitchItem { objectName: "servicesSwitch_weather_enabled"; Layout.fillWidth: true
                    text: "Weather"; checked: control.draft.weather_enabled
                    onCheckedChanged: if (control.loaded && control.draft.weather_enabled !== checked) control.setWeatherEnabled(checked) }
QOptionItem { objectName: "servicesLocation"; Layout.fillWidth: true
                    name: "Location"; childText: control.shortValue(control.draft.weather_location || "Choose a city"); onClicked: control.navigate("search") }
QOptionItem { objectName: "servicesWeatherProvider"; Layout.fillWidth: true
                    name: "Provider"; childText: control.weatherProviderName(); onClicked: control.navigate("weatherProvider") }
QOptionItem { objectName: "servicesUnits"; Layout.fillWidth: true
                    name: "Temperature"; childText: control.draft.weather_temperature_unit === "celsius" ? "°C" : "°F"; onClicked: control.change("weather_temperature_unit", control.draft.weather_temperature_unit === "celsius" ? "fahrenheit" : "celsius") }
QButton { objectName: "servicesWeatherNow"; Layout.fillWidth: true; radius: 22; text: "Update now"
                    enabled: !control.dirty && control.draft.weather_enabled; onClicked: control.updateWeather() }
                    Item { Layout.preferredHeight: 0 }
                }
            }
        }
    }
    Component { id: advancedPage
        QChildPage { property string screenName: "advanced"; title: "Advanced"; onExited: control.mainStackView.pop()
            QFlickable { objectName: "servicesForm_advanced"; anchors.fill: parent; anchors.bottomMargin: 140
                contentHeight: contentColumn.height
                ColumnLayout { id: contentColumn; x: 30; width: parent.width - 60; spacing: 12
                    enabled: control.loaded && !control.applying
                    QText { text: "Time"; font.pixelSize: 26; opacity: 0.6 }
                Repeater { model: control.timeFields
                    delegate: QOptionItem { objectName: "servicesField_" + modelData.key; Layout.fillWidth: true
                        name: modelData.label; childText: control.shortValue(control.draft[modelData.key])
                        onClicked: control.editField(modelData.key, modelData.label, modelData.hint) }
                }
                QText { text: "Weather"; font.pixelSize: 26; opacity: 0.6 }
                Repeater { model: control.weatherFields
                    delegate: QOptionItem { objectName: "servicesField_" + modelData.key; Layout.fillWidth: true
                        name: modelData.label; childText: control.shortValue(control.draft[modelData.key])
                        onClicked: control.editField(modelData.key, modelData.label, modelData.hint) }
                }
QOptionItem { objectName: "servicesGeocoder"; Layout.fillWidth: true
                    name: "Lookup URL"; childText: control.shortValue(control.geocoderUrl); onClicked: control.editField("geocoder_url", "Lookup URL", "HTTPS geocoding API URL") }
QTextArea { objectName: ""; Layout.fillWidth: true; Layout.preferredHeight: contentHeight
                    font.pixelSize: 23; textFormat: Text.PlainText; opacity: 0.7; text: "You can also enter the coordinates manually above." }
                QText { text: "Permissions"; font.pixelSize: 26; opacity: 0.6 }
QSwitchItem { objectName: "servicesSwitch_dns_enabled"; Layout.fillWidth: true
                    text: "DNS lookups"; checked: control.draft.dns_enabled
                    onCheckedChanged: if (control.loaded && control.draft.dns_enabled !== checked) control.change("dns_enabled", checked) }
                Repeater { model: control.dnsFields
                    delegate: QOptionItem { objectName: "servicesField_" + modelData.key; Layout.fillWidth: true
                        name: modelData.label; childText: control.shortValue(control.draft[modelData.key])
                        onClicked: control.editField(modelData.key, modelData.label, modelData.hint) }
                }
QSwitchItem { objectName: "servicesSwitch_allow_dns_rotation"; Layout.fillWidth: true
                    text: "Follow server IP changes"; checked: control.draft.allow_dns_rotation
                    onCheckedChanged: if (control.loaded && control.draft.allow_dns_rotation !== checked) control.change("allow_dns_rotation", checked) }
QSwitchItem { objectName: "servicesSwitch_allow_custom_lan"; Layout.fillWidth: true
                    text: "Allow private LAN servers"; checked: control.draft.allow_custom_lan
                    onCheckedChanged: if (control.loaded && control.draft.allow_custom_lan !== checked) control.change("allow_custom_lan", checked) }
QOptionItem { objectName: "servicesDetails"; Layout.fillWidth: true
                    name: "Details"; childText: "Status & connections"; onClicked: control.navigate("details") }
QButton { objectName: "servicesReload"; Layout.fillWidth: true; radius: 22; text: "Reload saved settings"
                    enabled: true; onClicked: control.loadConfig() }
QTextArea { objectName: ""; Layout.fillWidth: true; Layout.preferredHeight: contentHeight
                    font.pixelSize: 23; textFormat: Text.PlainText; opacity: 0.7; text: "Reload removes the changes that you did not save." }
                    Item { Layout.preferredHeight: 12 }
                }
            }
        }
    }
    Component { id: approvalPage
        QChildPage { property string screenName: "approval"; title: "Review access"; onExited: control.mainStackView.pop()
            QFlickable { objectName: "servicesForm_approval"; anchors.fill: parent; anchors.bottomMargin: 140
                contentHeight: contentColumn.height
                ColumnLayout { id: contentColumn; x: 30; width: parent.width - 60; spacing: 22
                    enabled: control.loaded && !control.applying
                    QTextArea { objectName: "servicesRequestedAccess"; Layout.fillWidth: true; Layout.preferredHeight: contentHeight
                        font.pixelSize: 30; textFormat: Text.PlainText; text: control.approvalSummary() }
                    QTextArea { Layout.fillWidth: true; Layout.preferredHeight: contentHeight; font.pixelSize: 23; opacity: 0.65; textFormat: Text.PlainText
                        text: control.draft.dns_enabled && control.draft.allow_dns_rotation ? "Allow these services and their DNS address updates." : "Allow only these selected services." }
                    QOptionItem { objectName: "servicesConnectionDetails"; Layout.fillWidth: true; name: "Connection details"
                        onClicked: control.connectionDetails(false) }
                    Item { Layout.preferredHeight: 12 }
                }
            }
        }
    }
    Component { id: connectionsPage
        QChildPage { property string screenName: "connections"; property string body
            title: "Connection details"; onExited: control.mainStackView.pop()
            QFlickable { anchors.fill: parent; anchors.margins: 30; contentHeight: detailText.contentHeight
                QTextArea { id: detailText; width: parent.width; height: contentHeight
                    font.pixelSize: 24; textFormat: Text.PlainText; text: body }
            }
        }
    }
    Component { id: detailsPage
        QChildPage { property string screenName: "details"; title: "Details"; onExited: control.mainStackView.pop()
            QFlickable { objectName: "servicesForm_details"; anchors.fill: parent; anchors.bottomMargin: 140
                contentHeight: contentColumn.height
                ColumnLayout { id: contentColumn; x: 30; width: parent.width - 60; spacing: 12
                    enabled: control.loaded && !control.applying
                    QTextArea { Layout.fillWidth: true; Layout.preferredHeight: contentHeight; font.pixelSize: 23; textFormat: Text.PlainText
                        visible: !!control.searchDetails; text: "Location lookup\n" + control.searchDetails }
                    QTextArea { objectName: "servicesDetailedFeedback"; Layout.fillWidth: true; Layout.preferredHeight: contentHeight
                    font.pixelSize: 23; textFormat: Text.PlainText; opacity: 0.7; text: control.statusData.message || control.statusReadError || "No recent save result." }
QTextArea { objectName: "servicesTimeStatus"; Layout.fillWidth: true; Layout.preferredHeight: contentHeight
                    font.pixelSize: 23; textFormat: Text.PlainText; opacity: 0.7; text: "Time\n" + control.serviceStatus("time") }
QTextArea { objectName: "servicesWeatherStatus"; Layout.fillWidth: true; Layout.preferredHeight: contentHeight
                    font.pixelSize: 23; textFormat: Text.PlainText; opacity: 0.7; text: "Weather\n" + control.serviceStatus("weather") }
QTextArea { objectName: "servicesDnsStatus"; Layout.fillWidth: true; Layout.preferredHeight: contentHeight
                    font.pixelSize: 23; textFormat: Text.PlainText; opacity: 0.7; text: "DNS\n" + control.serviceStatus("dns") }
QTextArea { objectName: "servicesApprovedServers"; Layout.fillWidth: true; Layout.preferredHeight: contentHeight
                    font.pixelSize: 23; textFormat: Text.PlainText; opacity: 0.7; text: "Approved connections\n" + control.approvedServers() }
QTextArea { objectName: "servicesWeatherReadings"; Layout.fillWidth: true; Layout.preferredHeight: contentHeight
                    font.pixelSize: 23; textFormat: Text.PlainText; opacity: 0.7; text: control.weatherReadings() }
                    QTextArea { Layout.fillWidth: true; Layout.preferredHeight: contentHeight; font.pixelSize: 21; textFormat: Text.PlainText; opacity: 0.55
                        text: (control.weatherProviderName() === "Open-Meteo" ? "Weather: Open-Meteo · open-meteo.com\n" : "") + (control.searchHost() === "geocoding-api.open-meteo.com" ? "Location data: GeoNames · geonames.org\n" : "") + "Open-Meteo logs may retain query data for 90 days." }
                    Item { Layout.preferredHeight: 12 }
                }
            }
        }
    }
    Component { id: timeProviders
        QChildPage { property string screenName: "timeProvider"; title: "Time provider"; onExited: control.mainStackView.pop()
            QFlickable { objectName: "servicesForm_timeProvider"; anchors.fill: parent; anchors.bottomMargin: 20
                contentHeight: contentColumn.height
                ColumnLayout { id: contentColumn; x: 30; width: parent.width - 60; spacing: 12
                    enabled: control.loaded && !control.applying
                    QOptionItem { objectName: "servicesPreset_Cloudflare"; Layout.fillWidth: true
                    name: "Cloudflare"; childText: ""; onClicked: { control.selectTimeProvider("Cloudflare"); control.mainStackView.pop(); } }
QOptionItem { objectName: "servicesPreset_Custom"; Layout.fillWidth: true
                    name: "Custom"; childText: ""; onClicked: { control.selectTimeProvider("Custom"); control.mainStackView.pop(); control.editField("time_servers", "Time servers", "IPv4 addresses or hostnames, separated by commas"); } }
                    Item { Layout.preferredHeight: 12 }
                }
            }
        }
    }
    Component { id: weatherProviders
        QChildPage { property string screenName: "weatherProvider"; title: "Weather provider"; onExited: control.mainStackView.pop()
            QFlickable { objectName: "servicesForm_weatherProvider"; anchors.fill: parent; anchors.bottomMargin: 20
                contentHeight: contentColumn.height
                ColumnLayout { id: contentColumn; x: 30; width: parent.width - 60; spacing: 12
                    enabled: control.loaded && !control.applying
                    QOptionItem { objectName: "servicesPreset_OpenMeteo"; Layout.fillWidth: true
                    name: "Open-Meteo"; childText: ""; onClicked: { control.selectWeatherProvider("Open-Meteo"); control.mainStackView.pop(); } }
QOptionItem { objectName: "servicesPreset_Custom"; Layout.fillWidth: true
                    name: "Custom"; childText: ""; onClicked: { control.selectWeatherProvider("Custom"); control.mainStackView.pop(); control.editField("weather_url", "Weather URL", "HTTPS weather API URL"); } }
                    Item { Layout.preferredHeight: 12 }
                }
            }
        }
    }
    Component { id: searchPage
        QChildPage { property string screenName: "search"; title: "City or postcode"; onExited: control.mainStackView.pop()
            Keyboard { id: searchKeyboard; objectName: "servicesSearchKeyboard"; anchors.fill: parent
                text: control.searchQuery; inputPlaceholderText: "City or postcode"; ensureButtonText: "Search"
                // The stock keyboard uses charLimit as the minimum length.
                charLimit: 2; passwdEnable: false; isPwdEchoMode: false
                onEditFinished: control.requestSearch(text)
            }
        }
    }
    Component { id: searchPermissionPage
        QChildPage { property string screenName: "searchPermission"; title: "Allow this search?"; onExited: control.mainStackView.pop()
            QFlickable { objectName: "servicesForm_searchPermission"; anchors.fill: parent; anchors.bottomMargin: 20
                contentHeight: contentColumn.height
                ColumnLayout { id: contentColumn; x: 30; width: parent.width - 60; spacing: 14
                    enabled: control.loaded && !control.applying
                    QTextArea { Layout.fillWidth: true; Layout.preferredHeight: contentHeight
                        font.pixelSize: 30; textFormat: Text.PlainText; text: "“" + control.searchQuery + "”" }
                    QTextArea { Layout.fillWidth: true; Layout.preferredHeight: contentHeight; font.pixelSize: 26; textFormat: Text.PlainText
                        text: (control.searchHost() === "geocoding-api.open-meteo.com" ? "Open-Meteo" : control.searchHost()) +
                            (control.searchNeedsDns() ? "\nDNS · " + control.dnsProviderLabel() : "\nNo DNS needed") }
                    QTextArea { Layout.fillWidth: true; Layout.preferredHeight: contentHeight; font.pixelSize: 23; opacity: 0.65; textFormat: Text.PlainText
                        text: "Sends your query and IP address for this search only." }
                    QOptionItem { objectName: "servicesSearchDetails"; Layout.fillWidth: true; name: "Connection details"
                        onClicked: control.connectionDetails(true) }
                    QButton { objectName: "servicesSearchApprove"; Layout.fillWidth: true; radius: 22; text: "Allow search once"
                        enabled: !control.searching; onClicked: control.searchNow() }
                    QButton { objectName: "servicesSearchCancel"; Layout.fillWidth: true; radius: 22; text: "Cancel"
                        onClicked: control.mainStackView.pop() }
                }
            }
        }
    }
    Component { id: searchResultsPage
        QChildPage { property string screenName: "searchResults"; title: "Choose location"; onExited: control.mainStackView.pop()
            QFlickable { objectName: "servicesForm_searchResults"; anchors.fill: parent; anchors.bottomMargin: 20
                contentHeight: contentColumn.height
                ColumnLayout { id: contentColumn; x: 30; width: parent.width - 60; spacing: 12
                    enabled: control.loaded && !control.applying
                    QTextArea { objectName: "servicesSearchFeedback"; Layout.fillWidth: true; Layout.preferredHeight: contentHeight
                    font.pixelSize: 25; textFormat: Text.PlainText; text: control.searchFeedback; visible: control.searchResults.length === 0 }
                Repeater { model: control.searchResults
                    delegate: Rectangle {
                        objectName: "servicesSearchResult_" + index; signal clicked()
                        Layout.fillWidth: true; Layout.preferredHeight: Math.max(104, resultLabels.height + 32); radius: 22; color: "#1AFFFFFF"
                        Column { id: resultLabels; x: 24; anchors.verticalCenter: parent.verticalCenter; width: parent.width - 48; spacing: 6
                            QText { objectName: "servicesResultName_" + index; width: parent.width; textFormat: Text.PlainText; text: modelData.name; font.pixelSize: 30; wrapMode: Text.WordWrap; maximumLineCount: 2 }
                            QText { width: parent.width; textFormat: Text.PlainText; text: (modelData.admin1 ? modelData.admin1 + ", " : "") + (modelData.country || ""); font.pixelSize: 22; opacity: 0.6; wrapMode: Text.WordWrap; maximumLineCount: 2 }
                        }
                        MouseArea { anchors.fill: parent; onClicked: parent.clicked() }
                        onClicked: control.selectLocation(modelData)
                    }
                }
                QOptionItem { objectName: "servicesSearchErrorDetails"; Layout.fillWidth: true; name: "Details"
                    visible: !!control.searchDetails && control.searchResults.length === 0
                    onClicked: control.connectionDetails(true) }
QButton { objectName: "servicesSearchAgain"; Layout.fillWidth: true; radius: 22; text: "Search again"
                    enabled: !control.searching; onClicked: { control.mainStackView.pop(); } }
                QTextArea { objectName: "servicesLocationAttribution"; Layout.fillWidth: true; Layout.preferredHeight: contentHeight
                    visible: control.searchHost() === "geocoding-api.open-meteo.com" && control.searchResults.length > 0
                    font.pixelSize: 21; opacity: 0.55; textFormat: Text.PlainText; text: "GeoNames via Open-Meteo" }
                    Item { Layout.preferredHeight: 12 }
                }
            }
        }
    }
    Component {
        id: timeZones
        QChildPage {
            property string screenName: "zones"
            objectName: "servicesTimeZones"; title: "Time zone"; onExited: control.mainStackView.pop()
            ListView {
                id: timeZoneList; objectName: "servicesTimeZoneList"
                anchors.fill: parent; anchors.leftMargin: 30; anchors.rightMargin: 30; anchors.bottomMargin: 20
                spacing: 14; clip: true; model: datetimeManager.timezoneConfig.timezoneList
                header: QOptionItem {
                    objectName: "servicesCurrentTimeZone"; width: timeZoneList.width
                    visible: !!control.currentTimeZone() && control.timeZoneIndex(control.currentTimeZone()) < 0
                    height: visible ? implicitHeight : 0
                    name: control.zoneLabel(control.currentTimeZone()); childText: "Current"
                    onClicked: { control.change("time_timezone", control.currentTimeZone()); control.mainStackView.pop(); }
                }
                delegate: QOptionItem {
                    objectName: "servicesTimeZone_" + modelData.name; width: ListView.view.width
                    name: control.zoneLabel(modelData.name); childText: modelData.gmt || ""
                    onClicked: { control.change("time_timezone", modelData.name); control.mainStackView.pop(); }
                }
            }
        }
    }
    Component {
        id: editor
        QChildPage {
            property string screenName: "editor"
            property string fieldKey; property string label; property string hint
            title: label; onExited: control.mainStackView.pop()
            Keyboard {
                id: keyboard; objectName: "servicesKeyboard"; anchors.fill: parent
                text: fieldKey === "geocoder_url" ? control.geocoderUrl : String(control.draft[fieldKey] || "")
                inputPlaceholderText: hint; ensureButtonText: "Done"
                charLimit: 0; passwdEnable: false; isPwdEchoMode: false
                onEditFinished: {
                    if (fieldKey === "geocoder_url") control.setGeocoder(keyboard.text);
                    else control.change(fieldKey, keyboard.text);
                    control.mainStackView.pop();
                }
            }
        }
    }
}
