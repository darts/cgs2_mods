#!/usr/bin/env python3
"""Build an isolated Internet Services Qt smoke test from the audited stock app.

The generated test mocks every command, reads only bundled JSON fixtures, and
writes its QSettings request and screenshots inside its chosen runtime directory.
It never starts the real helper or accesses the network. Extracted proprietary
resources must remain private. --check runs JS logic checks on the host; the
generated Main.qml additionally verifies real Qt.btoa, native controls and layout.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

from patch_app import ROOT, STOCK_SHA256, SERVICES_STARTUP, closing_brace, resources, transform

SOURCE = ROOT / "ui/InternetServices.qml"
FIELDS = {
    "time_servers": "time.example.test,192.0.2.15",
    "time_port": "1123",
    "time_interval_hours": "12",
    "time_timezone": "Etc/UTC",
    "weather_url": "https://api.open-meteo.com/v1/forecast",
    "weather_latitude": "48.14",
    "weather_longitude": "11.58",
    "weather_location": "München ' $ ` ; \" test",
    "weather_timezone": "Europe/Berlin",
    "weather_temperature_unit": "fahrenheit",
    "weather_interval_minutes": "60",
    "dns_servers": "192.0.2.53,192.0.2.54",
    "dns_port": "5353",
}
SWITCHES = ["time_enabled", "weather_enabled", "dns_enabled",
            "allow_dns_rotation", "allow_custom_lan"]
SNAPSHOT = {
    "updated_at": "2026-10-20T12:00:00Z", "source": "api.open-meteo.com",
    "latitude": 48.14, "longitude": 11.58, "timezone": "Europe/Berlin",
    "temperature_unit": "°F", "wind_speed_unit": "km/h",
    "current": {"time": "2026-10-20T14:00", "temperature": 68,
                "humidity": 45, "weather_code": 2, "is_day": True, "wind_speed": 8},
    "daily": [{"date": "2026-10-21", "weather_code": 61,
               "minimum_temperature": 50, "maximum_temperature": 70,
               "precipitation_probability": 25}],
}

HOST_CHECK = r'''
const vm = require("node:vm");
const assert = require("node:assert/strict");
const input = JSON.parse(require("node:fs").readFileSync(process.argv[2], "utf8"));
let count = 0;
function check(condition, label) { assert.ok(condition, label); count++; }
const commands = [];
const zones = [{name:"Etc/UTC"}, {name:"Europe/Amsterdam"}, {name:"Pacific/Auckland"}];
const zoneConfig = {timezoneList: zones, timezoneIndex: -1};
Object.defineProperty(zoneConfig, "name", {get() {return this.timezoneIndex < 0 ? "Europe/Berlin" : zones[this.timezoneIndex].name;}});
// Qt.labs.Settings property writes reach QSettings on a delayed timer. sync()
// alone only flushes its previous cached value; setValue() must update it first.
const requestStore = {pending:"", cached:"previous request", persisted:"previous request",
    setValue(key,value) {assert.equal(key,"payload"); this.cached = value;},
    sync() {this.persisted = this.cached; this.synced = true;}};
Object.defineProperty(requestStore,"payload",{get() {return this.pending;}, set(value) {this.pending=value;}});
const searchStore = {payload:"",persisted:"",setValue(key,value){this.persisted=value;},sync(){}};
const locationStore = {url:"",setValue(key,value){this[key]=value;},sync(){}};
const context = {
    console, Date, Math, JSON, Array, String, Number, isFinite,
    encodeURIComponent, unescape,
    Qt: {btoa: value => Buffer.from(value, "binary").toString("base64")},
    datetimeManager: {timezoneConfig: zoneConfig},
    requestStore, searchStore, locationStore,
    searchQuery:"",searchId:"",searching:false,searchFeedback:"",searchResults:[],
    geocoderUrl:"https://geocoding-api.open-meteo.com/v1/search", showError(message){this.lastError=message;},
    timePage:"time",weatherPage:"weather",advancedPage:"advanced",approvalPage:"approval",detailsPage:"details",
    searchPage:"search",searchPermissionPage:"searchPermission",searchResultsPage:"searchResults",
    timeProviders:"timeProvider",weatherProviders:"weatherProvider",timeZones:"zones",editor:"editor",connectionsPage:"connections",
    mainStackView:{routes:[],push(name,args){this.routes.push({name,args});},pop(){this.routes.pop();}},
    developerController: {runCommand(command) {
        if (command.endsWith(" apply")) {
            requestStore.readByHelper = requestStore.persisted;
            check(requestStore.persisted === requestStore.payload,
                "new payload is on disk before helper invocation despite delayed property writes");
        }
        if (command.endsWith(" geocode")) check(searchStore.persisted === searchStore.payload,
            "search payload persisted before helper");
        commands.push(command);
    }},
    loaded: true, dirty: false, applying: false, applyUnconfirmed:false,
    submittedId:"", submittedJson:"", statusReadError:"", lastRequestTime:0,
    feedback:"", statusData:{}, screen:"",
};
context.control = context;
vm.createContext(context);
vm.runInContext(input.functions.join("\n"), context);
context.draft = context.defaults();
check(context.draft.time_timezone === "Europe/Berlin", "preserve native time zone");
check(context.timeZoneIndex("Europe/Berlin") === -1 && context.validate() === "",
    "current native Berlin remains valid when curated list only includes Amsterdam");
for (const key of input.switches) check(context.draft[key] === false, key + " defaults off");
for (const [key, value] of Object.entries(input.fields)) {
    context.change(key, value);
    check(context.draft[key] === value, key + " editable");
}
for (const key of input.switches) context.change(key, true);
check(context.validate() === "", "valid complete draft");
for (const [key, invalid] of [
    ["time_port","0"], ["time_port","65536"], ["time_interval_hours","0"],
    ["weather_latitude","91"], ["weather_latitude",""], ["weather_latitude","NaN"],
    ["weather_longitude","-181"], ["weather_temperature_unit","kelvin"],
    ["weather_interval_minutes","0"], ["weather_url","file:///etc/passwd"],
    ["dns_port","65536"], ["time_timezone","Made/Up"]
]) {
    const previous = context.draft[key];
    context.change(key, invalid);
    context.save();
    check(commands.length === 0 && context.validate() !== "", key + " rejects " + invalid);
    context.change(key, previous);
}
context.syncNow(); context.updateWeather();
check(commands.length === 0, "unsaved actions blocked");
context.save();
check(requestStore.readByHelper === requestStore.payload, "helper reads the current on-disk draft, not the previous one");
check(commands.length === 1 && commands[0].endsWith(" apply"), "fixed apply command");
const request = JSON.parse(Buffer.from(context.requestStore.payload, "base64").toString("utf8"));
for (const [key, value] of Object.entries(input.fields)) check(request[key] === value, key + " serialized");
for (const key of input.switches) check(request[key] === true, key + " consent serialized");
check(request.request_id === context.submittedId && request.version === 1, "request identity");
check(context.requestStore.synced === true, "QSettings synchronized before command");
check(zoneConfig.name === "Europe/Berlin", "native timezone unchanged before approval");
context.acceptStatus({last_apply_id:"stale",apply_ok:true,message:"old"});
check(context.applying, "stale status cannot complete apply");
context.acceptStatus({last_apply_id:context.submittedId,apply_ok:false,message:"Denied"});
check(!context.applying && context.dirty && context.feedback === "Could not save. Open Details.", "failed apply preserves draft");
const firstId = context.submittedId;
context.save();
check(context.submittedId !== firstId, "retry has fresh request id");
context.acceptStatus({last_apply_id:context.submittedId,apply_ok:true,message:"Saved",
    approved_servers:["udp:192.0.2.15:1123"],
    weather:{message:"Cached",snapshot:input.snapshot}});
check(!context.applying && !context.dirty, "matching success completes apply");
check(zoneConfig.name === "Etc/UTC", "approved timezone updates native selection");
context.syncNow(); context.updateWeather();
check(commands[2] === "/usr/local/sbin/cgs2-services time-now", "fixed time command");
check(commands[3] === "/usr/local/sbin/cgs2-services weather-now", "fixed weather command");
check(context.approvedServers() === "udp:192.0.2.15:1123", "actual approved endpoint display");
check(context.requestedAccess().includes("DNS rotation is approved"), "rotation consent preview");
const weather = context.weatherReadings();
check(weather.includes("68 °F") && weather.includes("Partly cloudy") &&
      weather.includes("2026-10-21") && weather.includes("Rain 25%"), "current and forecast display");
check(!context.shortValue("<img src='http://bad'>").includes("<"), "option preview has no rich text");
context.statusData = {weather:{snapshot:{current:{temperature:null},daily:[]}}};
check(context.weatherReadings().includes("Unavailable"), "missing reading is not zero");
for (const key of input.switches) context.change(key, false);
context.save();
const disabled = JSON.parse(Buffer.from(context.requestStore.payload,"base64").toString("utf8"));
check(input.switches.every(key => disabled[key] === false), "revoke all services and consents");
context.acceptConfig(null);
check(!context.draft.time_enabled && !context.draft.weather_enabled && !context.draft.dns_enabled,
      "missing configuration defaults off");
context.applying = false;
zoneConfig.timezoneIndex = -1;
context.acceptConfig(null);
context.save();
context.acceptStatus({last_apply_id:context.submittedId,apply_ok:true});
check(!context.dirty && zoneConfig.timezoneIndex === -1 && zoneConfig.name === "Europe/Berlin",
    "saving current zone absent from list never substitutes another native index");
context.save();
const pendingDraft = JSON.stringify(context.draft);
context.statusReadError = "Service status is unavailable.";
context.expireApply();
check(!context.applying && context.applyUnconfirmed && context.dirty && JSON.stringify(context.draft) === pendingDraft,
    "timeout unlocks controls and keeps draft without claiming failure or success");
check(context.feedback.includes("not confirmed") && context.feedback.includes("Your edits stay"),
    "timeout explains missing status");
context.change("time_timezone","Pacific/Auckland");
context.acceptStatus({last_apply_id:context.submittedId,apply_ok:true,message:"Late save"});
check(context.dirty && context.draft.time_timezone === "Pacific/Auckland" && zoneConfig.name === "Europe/Berlin",
    "late success preserves newer edits and applies only the submitted timezone");
context.save();
context.expireApply();
const expiredId = context.submittedId;
context.save();
check(context.submittedId !== expiredId && context.applying, "timeout retry gets a fresh request id");
context.acceptStatus({last_apply_id:expiredId,apply_ok:true});
check(context.applying, "old late result cannot complete a newer save");
context.acceptStatus({last_apply_id:context.submittedId,apply_ok:false,message:"Rejected"});
check(!context.applying && context.dirty, "retry failure remains editable");
const runCommand = context.developerController.runCommand;
context.developerController.runCommand = () => {throw new Error("command transport unavailable");};
context.save();
check(!context.applying && context.applyUnconfirmed && context.dirty, "command exception cannot lock the form");
context.developerController.runCommand = runCommand;
context.applying=false; context.acceptConfig(null);
check(context.zoneLabel("Europe/Berlin") === "Berlin" && context.zoneLabel("America/New_York") === "New York", "friendly time zone labels");
const beforeReview=commands.length;
context.reviewSave();
check(context.mainStackView.routes.at(-1).name === "approval" && commands.length === beforeReview, "Save opens review without applying");
context.mainStackView.pop();
check(commands.length === beforeReview, "cancelling approval has no command");
for (const name of ["time","weather","advanced","details","timeProvider","weatherProvider","zones"]){
    context.navigate(name);check(context.mainStackView.routes.at(-1).name === name,"route "+name);context.mainStackView.pop();
}
context.change("time_servers","192.0.2.8");context.change("weather_url","https://weather.example.test/forecast");
context.selectTimeProvider("Custom");context.selectWeatherProvider("Custom");
check(context.draft.time_servers === "192.0.2.8" && context.draft.weather_url === "https://weather.example.test/forecast", "Custom preserves entered endpoints");
context.selectTimeProvider("Cloudflare");context.selectWeatherProvider("Open-Meteo");
check(context.timeProviderName() === "Cloudflare" && context.weatherProviderName() === "Open-Meteo", "explicit preset selection");
context.change("dns_servers","192.0.2.53");context.setWeatherEnabled(true);
check(context.draft.dns_enabled && context.draft.allow_dns_rotation && context.draft.dns_servers === "192.0.2.53", "weather stages DNS using chosen resolvers");
check(commands.length === beforeReview, "presets grant no network access before approval");
context.change("weather_location","Test city");context.statusData={weather:{message:"DNS udp connection failed: network is unreachable"}}; context.dirty=false;
check(context.serviceSummary("weather") === "Enable Wi-Fi in Settings", "network failure is actionable");
context.change("weather_url","https://api.open-meteo.com.evil.test/v1/forecast");
check(context.weatherProviderName() === "Custom", "lookalike domain never presented as Open-Meteo");
context.setGeocoder("https://192.0.2.80/search");
check(!context.searchNeedsDns() && locationStore.url === context.geocoderUrl,"numeric lookup needs no DNS and custom endpoint persists separately");
context.setGeocoder("https://geocoding-api.open-meteo.com/v1/search");
context.requestSearch(" ");check(context.lastError === "Enter a city or postcode." && commands.length === beforeReview,"empty query makes no command");
context.change("weather_enabled",false);context.change("dns_enabled",false);context.change("allow_dns_rotation",false);
const savedBeforeSearch=JSON.stringify(context.draft);
context.requestSearch("Springfield");
check(context.mainStackView.routes.at(-1).name === "searchPermission" && commands.length === beforeReview, "search first opens one-time permission sheet");
context.mainStackView.pop();
check(commands.length === beforeReview, "cancelling search permission makes no helper call");
context.requestSearch("Springfield");context.searchNow();
check(commands.at(-1) === "/usr/local/sbin/cgs2-services geocode", "fixed geocode helper");
const search=JSON.parse(Buffer.from(searchStore.persisted,"base64").toString("utf8"));
check(search.endpoint === context.geocoderUrl && search.query === "Springfield" && search.approved === true &&
    search.allow_dns_rotation === true && search.dns_servers === "192.0.2.53", "exact one-search permission payload");
check(JSON.stringify(context.draft) === savedBeforeSearch && !context.draft.weather_enabled && !context.draft.dns_enabled && !context.draft.allow_dns_rotation,
    "one-shot search works with services disabled without saving or mutating their permissions");
context.acceptSearch({request_id:"stale",ok:true,results:[]});check(context.searching,"stale search result ignored");
context.acceptSearch({request_id:context.searchId,busy:true,ok:false});check(context.searching,"busy result does not finish search");
const place={name:"Springfield",admin1:"Illinois",country:"United States",latitude:39.8,longitude:-89.6,timezone:"America/Chicago"};
context.acceptSearch({request_id:context.searchId,ok:true,results:[place]});
check(!context.searching && context.searchResults.length === 1,"matching search result accepted");
const oldTimeZone=context.draft.time_timezone;
context.selectLocation(place);
check(context.draft.weather_location === "Springfield, Illinois, United States" && context.draft.weather_latitude === "39.8" &&
    context.draft.weather_timezone === "America/Chicago" && context.draft.time_timezone === oldTimeZone,"city selection fills weather only and distinguishes region/country");
check(commands.length === beforeReview+1,"result selection grants no ongoing permissions");
context.searching=true;context.expireSearch();check(!context.searching && context.searchFeedback.includes("timed out"),"search timeout remains actionable");
context.acceptSearch({request_id:context.searchId,ok:false,message:"x509: certificate has expired or is not yet valid"});
check(context.searchFeedback === "Sync time, then retry." && context.searchDetails.includes("x509"),"certificate clock problem is actionable and raw details retained");
context.acceptSearch({request_id:context.searchId,ok:false,message:"dial udp4: network is unreachable"});
check(context.searchFeedback === "Enable Wi-Fi in Settings.","lookup network error points to Wi-Fi");
context.connectionDetails(true);check(context.mainStackView.routes.at(-1).args.body.includes(context.geocoderUrl),"lookup exact endpoint remains in connection details");
context.connectionDetails(false);check(context.mainStackView.routes.at(-1).args.body.includes("DNS rotation"),"full service permissions remain accessible");
context.screen="approval";context.save();context.expireApply();const depthBeforeLate=context.mainStackView.routes.length;
context.acceptStatus({last_apply_id:context.submittedId,apply_ok:true});
check(context.mainStackView.routes.length===depthBeforeLate,"late success after timeout never navigates away from an in-progress editor");
context.draft.weather_enabled=true;context.dirty=false;context.statusData={weather:{last_success:new Date(Date.now()-7*60000).toISOString()}};
check(context.serviceSummary("weather")==="Updated 7m ago","service shows short freshness age");
context.draft.time_enabled=true;context.statusData={time:{message:"clock differs by over five minutes; use Sync now to authorize this correction"}};
check(context.serviceSummary("time")==="Tap Sync now","large clock correction has a short explicit approval action");
context.statusData={weather:{message:"weather HTTPS request failed: tls: failed to verify certificate: x509: certificate has expired or is not yet valid: current time 2017-08-09 is before 2026-10-19"}};
check(context.serviceSummary("weather")==="Sync time, then retry","weather certificate date failure points to the clock");
context.statusData={weather:{message:"weather HTTPS request failed: tls: failed to verify certificate: x509: certificate signed by unknown authority"}};
check(context.serviceSummary("weather")==="Check Details","other certificate failures are not misreported as clock errors");
console.log("CGS2_SERVICES_HOST_PASS " + count + " assertions (JS logic; real Qt tested by Main.qml)");
'''

HARNESS = r'''
import QtQuick 2.7
import QtQuick.Window 2.2
Window {
    id: window
    visible: true; width: 720; height: 720
    property var edits: @@EDITS@@
    property var switches: @@SWITCHES@@
    property var snapshot: @@SNAPSHOT@@
    property int assertions: 0
    QtObject { id: global; property string wifiMAC: "001122334455" }
    QtObject {
        id: fakeZone
        property var timezoneList: [
            {name:"Etc/UTC",gmt:"UTC",description:"UTC"},
            {name:"Europe/Amsterdam",gmt:"UTC+02",description:"Amsterdam, Berlin"},
            {name:"Pacific/Auckland",gmt:"UTC+13",description:"Auckland"}
        ]
        property int timezoneIndex: -1
        readonly property string name: timezoneIndex < 0 ? "Europe/Berlin" : timezoneList[timezoneIndex].name
    }
    QtObject { id: datetimeManager; property var timezoneConfig: fakeZone }
    QtObject {
        id: developerController
        property var commands: []
        property var lastRequest: ({})
        property var lastSearch: ({})
        function runCommand(command) {
            window.check(["/usr/local/sbin/cgs2-services start", "/usr/local/sbin/cgs2-services apply",
                "/usr/local/sbin/cgs2-services time-now", "/usr/local/sbin/cgs2-services weather-now",
                "/usr/local/sbin/cgs2-services geocode"].indexOf(command) >= 0, "fixed command only");
            commands.push(command);
            if (command.endsWith(" apply")) {
                lastRequest = window.readRequest("request.ini",page.submittedId);
                window.check(JSON.stringify(lastRequest) === JSON.stringify(JSON.parse(decodeURIComponent(escape(Qt.atob(page.requestPayload))))),
                    "settings persisted synchronously before helper");
            } else if (command.endsWith(" geocode")) {
                lastSearch = window.readRequest("search.ini",page.searchId);
                window.check(JSON.stringify(lastSearch) === JSON.stringify(JSON.parse(decodeURIComponent(escape(Qt.atob(page.searchPayload))))),
                    "search persisted synchronously before helper");
            }
        }
    }
    Rectangle { id: surface; anchors.fill: parent; color: "black"
        InternetServices { id: page; anchors.fill: parent }
    }
    function check(condition, label) {
        if (!condition) { console.error("CGS2_SERVICES_SMOKE_FAIL " + label); Qt.exit(1); throw new Error(label); }
        assertions++;
    }
    function readRequest(file,id) {
        var request=new XMLHttpRequest(); request.open("GET","file://"+@@RUNTIME@@+"/"+file,false);request.send();
        var match=/^payload=(.*)$/m.exec(request.responseText);
        check(match!==null,"request exists immediately"); var payload=match[1].trim();
        if(payload[0]==='"' && payload[payload.length-1]==='"')payload=payload.slice(1,-1);
        var value=JSON.parse(decodeURIComponent(escape(Qt.atob(payload))));
        check(value.request_id===id,"disk contains new request id at helper invocation"); return value;
    }
    function find(item,name) {
        if(item.objectName===name)return item;
        var children=item.children||[];
        for(var i=0;i<children.length;i++){var result=find(children[i],name);if(result)return result;}
        return null;
    }
    function findKey(item,label) {
        if(item.visible && item.text===label && typeof item.clicked==="function")return item;
        var children=item.children||[];
        for(var i=0;i<children.length;i++){var result=findKey(children[i],label);if(result)return result;}
        return null;
    }
    function click(name) {var item=find(page,name);check(item!==null,"control exists: "+name);item.clicked();}
    function current() {return page.mainStackView.currentItem;}
    function back() {page.mainStackView.pop();}
    function fit(item,label) {
        var pos=item.mapToItem(window.contentItem,0,0);
        check(pos.x>=0 && pos.y>=0 && pos.x+item.width<=721 && pos.y+item.height<=721,label+" fits 720px screen");
    }
    function screenshot(name) {surface.grabToImage(function(result){check(result.saveToFile(@@RUNTIME@@+"/"+name+".png"),"screenshot "+name);});}
    Timer {
        property int stage: 0
        property int editIndex: 0
        property bool editing: false
        interval: 400; repeat: true; running: true
        onTriggered: {
            if(stage===0){
                check(page.loaded,"canonical fixture loaded");
                check(typeof Qt.btoa==="function" && typeof Qt.atob==="function","real Qt base64 available");
                check(page.draft.time_timezone==="Europe/Berlin" && page.validate()==="","active Berlin preserved despite picker omission");
                check(developerController.commands.length===1,"opening page makes no search or apply request");
                fit(find(page,"servicesSave"),"sticky Save");screenshot("overview");
                stage=1;
            }else if(stage===1){click("servicesNavigate_time");stage=2;
            }else if(stage===2){
                check(page.screen==="time","time page route");
                var toggle=find(current(),"servicesSwitch_time_enabled");check(toggle!==null,"time switch");toggle.checked=true;
                check(page.draft.time_enabled,"time switch changes draft");fit(find(page,"servicesSave"),"Time Save");screenshot("time");
                stage=3;
            }else if(stage===3){back();page.navigate("weather");stage=4;
            }else if(stage===4){
                var weatherToggle=find(current(),"servicesSwitch_weather_enabled");weatherToggle.checked=true;
                check(page.draft.weather_enabled && page.draft.dns_enabled && page.draft.allow_dns_rotation,"weather stages required DNS");
                check(developerController.commands.length===1,"toggling services grants no access");
                fit(find(page,"servicesSave"),"Weather Save");screenshot("weather");stage=5;
            }else if(stage===5){click("servicesLocation");stage=6;
            }else if(stage===6){
                var keyboard=find(current(),"servicesSearchKeyboard");check(keyboard!==null,"city keyboard exists");
                var queries=["","B","Bo","Berlin","10115","München"];
                for(var mode=0;mode<2;mode++){
                    keyboard.isAbc=mode===0;
                    for(var q=0;q<queries.length;q++){
                        keyboard.text=queries[q];
                        var searchKey=findKey(keyboard,"Search");check(searchKey!==null,"visible Search key exists");
                        check(searchKey.enabled===(q>=2),"Search key for " + JSON.stringify(queries[q]) +
                            " in mode " + mode + " (minimum " + keyboard.charLimit + ", enabled " + searchKey.enabled + ")");
                    }
                }
                keyboard.isAbc=true;keyboard.text="Springfield";
                var submit=findKey(keyboard,"Search");check(submit!==null && submit.enabled,"city can be submitted");
                submit.clicked();check(developerController.commands.length===1,"typing and requesting search make no network call");stage=7;
            }else if(stage===7){
                check(page.screen==="searchPermission","one-search permission page");screenshot("search-permission");stage=8;
            }else if(stage===8){
                click("servicesSearchCancel");check(developerController.commands.length===1,"cancel search makes no command");
                page.requestSearch("Springfield");stage=9;
            }else if(stage===9){
                click("servicesSearchApprove");
                check(developerController.commands.length===2 && developerController.commands[1].endsWith(" geocode"),"confirmed one-search command only");
                check(developerController.lastSearch.approved && developerController.lastSearch.allow_dns_rotation,"search permission serialized");
                page.acceptSearch({request_id:page.searchId,ok:true,results:[
                    {name:"Springfield",admin1:"Illinois",country:"United States",latitude:39.8,longitude:-89.6,timezone:"America/Chicago"},
                    {name:"Springfield",admin1:"Massachusetts",country:"United States",latitude:42.1,longitude:-72.6,timezone:"America/New_York"}]});stage=10;
            }else if(stage===10){
                check(page.screen==="searchResults","results route");screenshot("search-results");stage=11;
            }else if(stage===11){click("servicesSearchResult_0");
                check(page.draft.weather_timezone==="America/Chicago" && page.draft.time_timezone==="Europe/Berlin","selection changes weather timezone only");
                back();page.navigate("advanced");stage=12;
            }else if(stage===12){
                check(page.screen==="advanced","advanced route");
                var form=find(current(),"servicesForm_advanced");check(form!==null && form.contentHeight>form.height,"all advanced fields reachable by scrolling above footer");fit(find(page,"servicesSave"),"Advanced Save");screenshot("advanced");
                for(var k=2;k<switches.length;k++){
                    var consent=find(current(),"servicesSwitch_"+switches[k]);check(consent!==null,"advanced consent exists "+switches[k]);consent.checked=true;
                    check(page.draft[switches[k]],"advanced consent editable "+switches[k]);
                }stage=13;
            }else if(stage===13){
                if(editIndex>=edits.length){stage=14;return;}
                var edit=edits[editIndex];
                if(!editing){var field=find(current(),"servicesField_"+edit[0]);check(field!==null,"field exists "+edit[0]);field.clicked();editing=true;}
                else {
                    if(edit[0]==="time_timezone")click("servicesTimeZone_"+edit[1]);
                    else {var editor=find(current(),"servicesKeyboard");check(editor!==null,"stock keyboard "+edit[0]);editor.text=edit[1];editor.editFinished();}
                    check(page.draft[edit[0]]===edit[1],"editable field "+edit[0]);editIndex++;editing=false;
                }
            }else if(stage===14){
                page.syncNow();page.updateWeather();check(developerController.commands.length===2,"unsaved actions blocked");
                click("servicesSave");check(developerController.commands.length===2,"Save opens review without apply");stage=15;
            }else if(stage===15){
                check(page.screen==="approval","approval route");fit(find(page,"servicesSave"),"Approve button");screenshot("approval");stage=16;
            }else if(stage===16){
                back();check(developerController.commands.length===2,"cancel review makes no command");page.reviewSave();stage=17;
            }else if(stage===17){
                click("servicesSave");check(page.applying && developerController.commands.length===3,"Approve submits current draft");
                for(var j=0;j<edits.length;j++)check(developerController.lastRequest[edits[j][0]]===edits[j][1],"serialized "+edits[j][0]);
                for(var n=0;n<switches.length;n++)check(developerController.lastRequest[switches[n]],"serialized consent "+switches[n]);
                page.acceptStatus({last_apply_id:"old",apply_ok:true});check(page.applying,"stale result ignored");
                page.acceptStatus({last_apply_id:page.submittedId,apply_ok:true,message:"Test success",approved_servers:["tcp:192.0.2.80:443"],weather:{snapshot:snapshot}});
                check(!page.applying && !page.dirty && fakeZone.name==="Etc/UTC","save succeeds and updates native timezone");
                page.syncNow();page.updateWeather();check(developerController.commands[3].endsWith(" time-now") && developerController.commands[4].endsWith(" weather-now"),"fixed action commands");
                page.save();stage=18;
            }else if(stage===18){
                check(!page.applying && page.applyUnconfirmed && page.dirty,"real elapsed timer unlocks unconfirmed save");
                page.change("time_timezone","Pacific/Auckland");page.acceptStatus({last_apply_id:page.submittedId,apply_ok:true});
                check(page.dirty && page.draft.time_timezone==="Pacific/Auckland" && fakeZone.name==="Etc/UTC","late acknowledgement preserves newer edits");
                stage=19;
            }else if(stage===19){
                page.acceptConfig(null);page.change("time_enabled",true);page.setWeatherEnabled(true);
                page.change("weather_latitude","52.52");page.change("weather_longitude","13.40");page.change("weather_location","Berlin");
                page.reviewSave();stage=20;
            }else if(stage===20){
                check(page.approvalSummary().indexOf("Cloudflare")>=0 && page.approvalSummary().indexOf("Quad9")>=0,"ordinary permission review names preset providers");
                screenshot("approval-defaults");stage=21;
            }else if(stage===21){click("servicesConnectionDetails");stage=22;
            }else if(stage===22){
                check(page.screen==="connections","exact connections accessible behind Details");screenshot("connection-details");stage=23;
            }else if(stage===23){
                back();
                page.searchId="long-label-fixture";
                page.acceptSearch({request_id:page.searchId,ok:true,results:[
                    {name:"San Fernando del Valle de Catamarca",admin1:"Provincia de Catamarca",country:"Argentina",latitude:-28.4,longitude:-65.7,timezone:"America/Argentina/Catamarca"},
                    {name:"Long place name with <literal> characters",admin1:"A region with a long descriptive name",country:"United Kingdom",latitude:53.2,longitude:-4.2,timezone:"Europe/London"}]});
                page.navigate("searchResults");stage=24;
            }else if(stage===24){
                check(find(current(),"servicesResultName_1").textFormat===Text.PlainText,"provider text renders literally");
                fit(find(current(),"servicesSearchResult_0"),"long location result");screenshot("search-results-long");stage=25;
            }else if(stage===25){
                check(find(current(),"servicesLocationAttribution").visible,"location attribution shown");
                page.acceptSearch({request_id:page.searchId,ok:false,message:"x509: certificate has expired or is not yet valid"});stage=26;
            }else if(stage===26){
                check(find(current(),"servicesSearchErrorDetails").visible,"failed search exposes Details directly");
                check(page.searchFeedback==="Sync time, then retry.","clock failure feedback is actionable");
                click("servicesSearchErrorDetails");stage=27;
            }else if(stage===27){
                check(page.screen==="connections" && current().body.indexOf("x509")>=0,"search error details retain original diagnostic");
                console.log("CGS2_SERVICES_SMOKE_PASS "+assertions+" assertions: routes, fields, explicit permissions, immediate disk writes, timeout and 720px layout");Qt.quit();
            }
        }
    }
    Timer {interval:60000;running:true;onTriggered:{console.error("CGS2_SERVICES_SMOKE_FAIL timeout");Qt.exit(1);}}
}
'''

SUMMARY_HARNESS = r'''
import QtQuick 2.7
import QtQuick.Window 2.2
import "summary"
Window {
    id: window
    visible: true; width: 720; height: 720
    property int assertions: 0
    property string openedPage: ""
    QtObject { id: global; property bool is24HModel: true }
    QtObject {
        id: datetimeManager
        property var datetimeData: ({dateStringFormat1:"Tuesday, 20 October",timeStringFormat1:"14:05",apString:"PM"})
    }
    QtObject {
        id: alarmManager
        property var delayedAlarms: []
        signal alarmsStatusChanged(bool triggering)
    }
    function openPage(url) { openedPage = url; }
    Rectangle {
        id: surface
        anchors.fill: parent; color: "black"
        SummaryView { id: summary; anchors.fill: parent }
    }
    function check(condition, label) {
        if (!condition) {
            console.error("CGS2_SUMMARY_SMOKE_FAIL " + label);
            Qt.exit(1); throw new Error(label);
        }
        assertions++;
    }
    function find(item, name) {
        if (item.objectName === name) return item;
        var children = item.children || [];
        for (var i = 0; i < children.length; i++) {
            var result = find(children[i], name);
            if (result) return result;
        }
        return null;
    }
    Timer {
        interval: 500; repeat: true; running: true
        property int stage: 0
        onTriggered: {
            var card = window.find(summary, "localWeatherCard");
            if (stage === 0) {
                window.check(card !== null && card.visible, "saved enabled weather appears");
                window.check(summary.localWeatherTitle().indexOf("68 °F") >= 0, "current temperature displayed");
                window.check(summary.localWeatherTitle().indexOf("Partly cloudy") >= 0, "weather condition displayed");
                var pos = card.mapToItem(window.contentItem, 0, 0);
                window.check(pos.x >= 0 && pos.y >= 0 && pos.x + card.width <= 720 && pos.y + card.height <= 720,
                    "card fits the 720px summary without clipping");
                var fresh = JSON.parse(JSON.stringify(summary.localWeatherSnapshot));
                fresh.updated_at = new Date(Date.now() - 60000).toISOString();
                summary.localWeatherSnapshot = fresh; summary.localWeatherNow = Date.now();
                window.check(summary.localWeatherAge().indexOf("Updated 1 min") >= 0 && summary.localWeatherAge().indexOf("Stale") < 0,
                    "fresh data age");
                surface.grabToImage(function(result) {
                    window.check(result.saveToFile(@@RUNTIME@@ + "/summary-fresh.png"), "fresh summary screenshot");
                });
            } else if (stage === 1) {
                var old = JSON.parse(JSON.stringify(summary.localWeatherSnapshot));
                old.updated_at = new Date(Date.now() - 14400000).toISOString();
                summary.localWeatherSnapshot = old; summary.localWeatherNow = Date.now();
                window.check(summary.localWeatherAge().indexOf("Stale") >= 0, "old weather marked stale");
                window.find(summary, "localWeatherOpen").clicked(null);
                window.check(window.openedPage === "qrc:/Modules/Location/LocationPage.qml", "card opens internet services");
                surface.grabToImage(function(result) {
                    window.check(result.saveToFile(@@RUNTIME@@ + "/summary-stale.png"), "stale summary screenshot");
                });
            } else if (stage === 2) {
                summary.localWeatherEnabled = false;
                window.check(!card.visible, "disabled weather hidden");
                summary.localWeatherEnabled = true;
                summary.localWeatherSnapshot = null;
                window.check(!card.visible, "missing snapshot hidden");
                console.log("CGS2_SUMMARY_SMOKE_PASS " + window.assertions + " assertions: local fixtures, current conditions, age, stale state, route and layout");
                Qt.quit();
            }
            stage++;
        }
    }
    Timer { interval: 10000; running: true; onTriggered: { console.error("CGS2_SUMMARY_SMOKE_FAIL timeout"); Qt.exit(1); } }
}
'''

STARTUP_HARNESS = r'''
import QtQuick 2.7
import QtQuick.Window 2.2
Window {
    visible: true; width: 720; height: 720
    QtObject {
        id: nativeZone
        property var timezoneList: [{name:"Etc/UTC"},{name:"Europe/Berlin"}]
        property int timezoneIndex: 1
        readonly property string name: timezoneList[timezoneIndex].name
    }
    QtObject {
        id: datetimeManager
        property bool isAutomatically: true
        property var timezoneConfig: nativeZone
    }
    QtObject {
        id: developerController
        property int calls: 0
        function runCommand(command) {
            if (command !== "/usr/local/sbin/cgs2-services start") {
                console.error("CGS2_STARTUP_SMOKE_FAIL unexpected command"); Qt.exit(1); return;
            }
            calls++;
        }
    }
    Item {
@@STARTUP@@
    }
    Timer {
        interval: 1800; running: true
        onTriggered: {
            if (developerController.calls !== 1 || nativeZone.name !== "Etc/UTC" || datetimeManager.isAutomatically) {
                console.error("CGS2_STARTUP_SMOKE_FAIL command count or native timezone"); Qt.exit(1); return;
            }
            console.log("CGS2_STARTUP_SMOKE_PASS fixed startup command, local canonical config, native timezone");
            Qt.quit();
        }
    }
}
'''


def check_host(source: str, node: str | None = None) -> None:
    functions = []
    for match in re.finditer(r"^    function [A-Za-z][A-Za-z0-9_]*\([^\n]*\) \{", source, re.M):
        start = match.start()
        end = closing_brace(source, source.index("{", start))
        functions.append(source[start:end])
    if len(functions) < 15:
        raise ValueError("did not extract the expected QML logic functions")
    calls = re.findall(r"developerController\.runCommand\(([^\n]*)\)", source)
    expected = {json.dumps("/usr/local/sbin/cgs2-services " + command)
                for command in ("start", "apply", "time-now", "weather-now", "geocode")}
    if set(calls) != expected or len(calls) != 5:
        raise ValueError("helper calls must be exactly the five fixed command literals")
    for key in FIELDS:
        if f'key: "{key}"' not in source:
            raise ValueError(f"missing editable field: {key}")
    for key in SWITCHES:
        if f'objectName: "servicesSwitch_{key}"' not in source:
            raise ValueError(f"missing consent control: {key}")
    executable = node or shutil.which("node")
    if not executable:
        raise ValueError("Node.js is required for host logic checks")
    with tempfile.TemporaryDirectory(prefix="cgs2-services-ui-") as tmp:
        folder = Path(tmp)
        (folder / "check.cjs").write_text(HOST_CHECK)
        (folder / "input.json").write_text(json.dumps({
            "functions": functions, "fields": FIELDS, "switches": SWITCHES, "snapshot": SNAPSHOT,
        }))
        subprocess.run([executable, str(folder / "check.cjs"), str(folder / "input.json")], check=True)


def build(stock: Path, output: Path, runtime: Path, source: str) -> None:
    binary = stock.read_bytes()
    if hashlib.sha256(binary).hexdigest() != STOCK_SHA256:
        raise ValueError("stock application does not match audited SHA256")
    output.mkdir(parents=True, exist_ok=True)
    qml = resources(binary)
    changes = transform(qml)
    asset_url = (runtime / "assets").as_uri() + "/"
    for name, entry in qml.items():
        if not name.startswith("/Qing/"):
            continue
        payload = changes.get(name, entry["payload"]).decode()
        payload = payload.replace('"qrc:/', '"' + asset_url)
        path = output / "imports" / name.lstrip("/")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(payload)
    images = resources(binary, tree_offset=0x26F8DB, tree_size=7018,
                       name_offset=0x271445, data_offset=0x272E09, data_size=1203381)
    for name, entry in images.items():
        path = output / "assets" / name.lstrip("/")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(entry["payload"])
    snow = output / "imports/Qing/Snow"
    snow.mkdir(parents=True, exist_ok=True)
    (snow / "qmldir").write_text("module Qing.Snow\nsingleton WiFiState 1.0 WiFiState.qml\n")
    (snow / "WiFiState.qml").write_text("pragma Singleton\nimport QtQuick 2.0\nQtObject {}\n")
    substitutions = {
        "/data/etc/cgs2-services-draft.ini": str(runtime / "request.ini"),
        "file:///etc/cgs2-privacy/services.json": (runtime / "config.json").as_uri(),
        "file:///run/cgs2-services/status.json": (runtime / "status.json").as_uri(),
        # Initial XHR fixtures are read normally. Synthetic status events then
        # exercise request-ID matching without a fixture poll overwriting them.
        "running: control.visible": "running: false",
        # Exercise the real QML timeout without waiting 45 seconds in the fixture.
        "interval: 45000; repeat: false; running: control.applying": "interval: 50; repeat: false; running: control.applying",
    }
    if source.count("/data/etc/cgs2-location-search.ini") != 2:
        raise ValueError("missing location Settings isolation anchors")
    source = source.replace("/data/etc/cgs2-location-search.ini", str(runtime / "search.ini"))
    source = source.replace("file:///run/cgs2-services/location-search.json", (runtime / "search-status.json").as_uri())
    source = source.replace("running: control.searching; onTriggered: control.loadSearch()", "running: false; onTriggered: control.loadSearch()")
    for original, replacement in substitutions.items():
        if source.count(original) != 1:
            raise ValueError(f"missing unique smoke isolation anchor: {original}")
        source = source.replace(original, replacement)
    (output / "InternetServices.qml").write_text(source)
    (output / "config.json").write_text(json.dumps({
        "version": 1, "time_timezone": "Europe/Berlin", "weather_timezone": "Europe/Berlin",
    }))
    (output / "status.json").write_text(json.dumps({"approved_servers": []}))
    (output / "search-status.json").write_text(json.dumps({}))
    harness = HARNESS.replace("@@EDITS@@", json.dumps(list(FIELDS.items()), ensure_ascii=False))
    harness = harness.replace("@@SWITCHES@@", json.dumps(SWITCHES))
    harness = harness.replace("@@SNAPSHOT@@", json.dumps(SNAPSHOT, ensure_ascii=False))
    harness = harness.replace("@@RUNTIME@@", json.dumps(str(runtime)))
    (output / "Main.qml").write_text(harness)
    for name, entry in qml.items():
        prefix = "/Main/Views/Summary/"
        if not name.startswith(prefix):
            continue
        payload = changes.get(name, entry["payload"]).decode()
        payload = payload.replace('"qrc:/', '"' + asset_url)
        # The card's navigation is recorded, never loaded; preserve its real
        # route while replacing image references and local fixture reads.
        payload = payload.replace(asset_url + "Modules/Location/LocationPage.qml",
                                  "qrc:/Modules/Location/LocationPage.qml")
        payload = payload.replace("file:///etc/cgs2-privacy/services.json",
                                  (runtime / "summary-config.json").as_uri())
        payload = payload.replace("file:///run/cgs2-services/status.json",
                                  (runtime / "summary-status.json").as_uri())
        target = output / "summary" / name[len(prefix):]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(payload)
    (output / "summary-config.json").write_text(json.dumps({
        "version": 1, "weather_enabled": True, "weather_interval_minutes": "30",
    }))
    (output / "summary-status.json").write_text(json.dumps({"weather": {"snapshot": SNAPSHOT}}))
    (output / "SummaryMain.qml").write_text(SUMMARY_HARNESS.replace("@@RUNTIME@@", json.dumps(str(runtime))))
    startup = SERVICES_STARTUP.replace("file:///etc/cgs2-privacy/services.json",
                                       (runtime / "startup-config.json").as_uri())
    (output / "startup-config.json").write_text(json.dumps({"version": 1, "time_timezone": "Etc/UTC"}))
    (output / "StartupMain.qml").write_text(STARTUP_HARNESS.replace("@@STARTUP@@", startup))
    print(f"Prepared isolated Qt smoke test: {output}")
    print(f"Runtime directory: {runtime}")
    print("Run with QML_DISABLE_DISK_CACHE=1, QML_XHR_ALLOW_FILE_READ=1, QML2_IMPORT_PATH=<runtime>/imports, "
          "QT_QUICK_BACKEND=software and qmlscene -platform offscreen <runtime>/Main.qml.")
    print("Also run SummaryMain.qml and StartupMain.qml with the same environment.")
    print("Require each harness's CGS2_*_SMOKE_PASS marker and no CGS2_*_SMOKE_FAIL marker; "
          "qmlscene can exit 0 after a failed assertion.")
    print("All command calls are mocked. No real helper, device configuration, or network endpoint is used.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stock_app", type=Path, nargs="?")
    parser.add_argument("output", type=Path, nargs="?")
    parser.add_argument("--check", action="store_true", help="run host JS logic checks only")
    parser.add_argument("--runtime-path", type=Path,
                        help="absolute runtime directory if different from the build output")
    parser.add_argument("--node", help="Node.js executable for host checks")
    args = parser.parse_args()
    source = SOURCE.read_text()
    check_host(source, args.node)
    if args.check:
        return
    if args.stock_app is None or args.output is None:
        parser.error("stock_app and output are required unless --check is used")
    output = args.output.resolve()
    runtime = args.runtime_path or output
    if not runtime.is_absolute():
        parser.error("--runtime-path must be absolute")
    build(args.stock_app, output, runtime, source)


if __name__ == "__main__":
    main()
