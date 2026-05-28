import { app } from "../../../scripts/app.js";
import { api } from "../../../scripts/api.js";

function chainCallback(object, property, callback) {
    if (!object) {
        return;
    }
    const original = object[property];
    if (original) {
        object[property] = function () {
            const result = original.apply(this, arguments);
            return callback.apply(this, arguments) ?? result;
        };
    } else {
        object[property] = callback;
    }
}

function parseStoredLocale(value) {
    if (!value) {
        return "";
    }
    try {
        return JSON.parse(value);
    } catch (_) {
        return String(value).replace(/^"(.*)"$/, "$1");
    }
}

function currentLocale(context = {}) {
    const candidates = [
        app?.ui?.settings?.getSettingValue?.("AGL.Locale"),
        localStorage.getItem("AGL.Locale"),
        parseStoredLocale(localStorage.getItem("Comfy.Settings.AGL.Locale")),
        app?.ui?.settings?.getSettingValue?.("Comfy.Locale"),
        parseStoredLocale(localStorage.getItem("Comfy.Settings.Comfy.Locale")),
        context.node?.title,
        context.nodeData?.display_name,
        context.nodeData?.displayName,
    ].filter(Boolean).map((v) => String(v));

    for (const candidate of candidates) {
        const value = candidate.toLowerCase();
        if (value.startsWith("zh") || /[\u4e00-\u9fff]/.test(candidate)) {
            return "zh";
        }
        if (value.startsWith("ja") || /[\u3040-\u30ff]/.test(candidate)) {
            return "ja";
        }
    }
    return "en";
}

const I18N = {
    zh: {
        upload: "选择/上传视频",
        uploadFailed: "视频上传失败",
        selectPlaceholder: "选择已有视频...",
        refresh: "刷新",
        noVideos: "没有找到视频文件",
        previewUnavailable: "预览仅支持 input/output/temp 内的视频；完整路径仍可手动输入。",
        helpTitle: "SUR 流式处理",
        helpLines: [
            "推荐共享 GPU 起步：RIFE，scale=2，插帧=2，chunk=19，桥接=1。",
            "19/1 会让非首块实际送入 FlashVSR 的帧数保持为 20，接近 VSRFI 参考设置。",
        ],
        widgets: {
            video_path: "视频文件",
            output_path: "输出路径",
            execute: "开始执行",
            scale: "放大倍率",
            interpolation_factor: "插帧倍率",
            vfi_method: "插帧方法",
            frames_per_chunk: "每块源帧数",
            bridge_frames: "桥接帧",
            max_tile_kilopixels: "VSR分块上限",
            max_gimm_kilopixels: "GIMM分块上限",
            skip_first_frames: "跳过开头帧",
            frame_load_cap: "最多处理帧",
        },
    },
    ja: {
        upload: "動画を選択/アップロード",
        uploadFailed: "動画のアップロードに失敗しました",
        selectPlaceholder: "既存の動画を選択...",
        refresh: "更新",
        noVideos: "動画ファイルが見つかりません",
        previewUnavailable: "プレビューは input/output/temp 内の動画のみ対応します。フルパスは手入力できます。",
        helpTitle: "SUR Stream",
        helpLines: [
            "Shared GPU baseline: RIFE, scale=2, interpolation=2, chunk=19, bridge=1.",
            "19/1 keeps non-first FlashVSR chunks at 20 frames, close to the reference VSRFI setup.",
        ],
        widgets: {
            video_path: "動画ファイル",
            output_path: "出力パス",
            execute: "実行",
            scale: "拡大倍率",
            interpolation_factor: "補間倍率",
            vfi_method: "補間方式",
            frames_per_chunk: "チャンクフレーム",
            bridge_frames: "ブリッジフレーム",
            max_tile_kilopixels: "VSR タイル上限",
            max_gimm_kilopixels: "GIMM 上限",
            skip_first_frames: "先頭スキップ",
            frame_load_cap: "処理上限",
        },
    },
    en: {
        upload: "choose/upload video",
        uploadFailed: "Video upload failed",
        selectPlaceholder: "choose existing video...",
        refresh: "refresh",
        noVideos: "No video files found",
        previewUnavailable: "Preview works for videos under input/output/temp; full paths can still be typed.",
        helpTitle: "SUR Stream",
        helpLines: [
            "Shared-GPU baseline: RIFE, scale=2, interpolation=2, chunk=19, bridge=1.",
            "19/1 keeps non-first FlashVSR chunks at 20 frames, close to the reference VSRFI setup.",
        ],
        widgets: {
            video_path: "Video File",
            output_path: "Output Path",
            execute: "Execute",
            scale: "Scale",
            interpolation_factor: "Interpolation",
            vfi_method: "VFI Method",
            frames_per_chunk: "Frames/Chunk",
            bridge_frames: "Bridge Frames",
            max_tile_kilopixels: "VSR Tile Limit",
            max_gimm_kilopixels: "GIMM Limit",
            skip_first_frames: "Skip Frames",
            frame_load_cap: "Frame Cap",
        },
    },
};

function text(context) {
    return I18N[currentLocale(context)] ?? I18N.en;
}

async function uploadVideo(file) {
    const body = new FormData();
    body.append("file", file, file.name);
    return await api.fetchApi("/sur/upload_video", {
        method: "POST",
        body,
    });
}

async function fetchVideos() {
    const resp = await api.fetchApi("/sur/list_videos");
    if (!resp?.ok) {
        return [];
    }
    const data = await resp.json();
    return Array.isArray(data?.videos) ? data.videos : [];
}

function setWidgetValue(widget, value) {
    widget.value = value;
    if (widget.options?.values && !widget.options.values.includes(value)) {
        widget.options.values.push(value);
    }
    widget.callback?.(value);
}

function formatBytes(bytes) {
    const value = Number(bytes || 0);
    if (value >= 1024 ** 3) {
        return `${(value / 1024 ** 3).toFixed(1)}GB`;
    }
    if (value >= 1024 ** 2) {
        return `${(value / 1024 ** 2).toFixed(0)}MB`;
    }
    return `${Math.max(1, Math.round(value / 1024))}KB`;
}

function mediaValue(item) {
    return item?.relative || item?.name || "";
}

function basename(value) {
    return String(value || "").replace(/\\/g, "/").split("/").filter(Boolean).pop() || "";
}

function subfolderFromValue(value) {
    const clean = String(value || "").replace(/\\/g, "/");
    const parts = clean.split("/").filter(Boolean);
    parts.pop();
    return parts.join("/");
}

function fileURL(path) {
    return api.fileURL ? api.fileURL(path) : path;
}

function viewUrlForItem(item) {
    if (!item) {
        return "";
    }
    const params = new URLSearchParams({
        filename: item.name || basename(mediaValue(item)),
        type: item.type || "input",
    });
    if (item.subfolder) {
        params.set("subfolder", item.subfolder);
    }
    return fileURL(`/view?${params.toString()}`);
}

function viewUrlForValue(value, videos) {
    const raw = String(value || "").trim();
    if (!raw) {
        return "";
    }
    const match = videos.find((item) => {
        const mv = mediaValue(item);
        return mv === raw || item.name === raw || item.path === raw;
    });
    if (match) {
        return viewUrlForItem(match);
    }
    if (raw.startsWith("/") || /^[A-Za-z]:[\\/]/.test(raw)) {
        return "";
    }
    const params = new URLSearchParams({
        filename: basename(raw),
        type: "input",
    });
    const subfolder = subfolderFromValue(raw);
    if (subfolder) {
        params.set("subfolder", subfolder);
    }
    return fileURL(`/view?${params.toString()}`);
}

function markDirty(node) {
    node.setDirtyCanvas?.(true, true);
    app.canvas.setDirty?.(true, true);
    app.graph.setDirtyCanvas?.(true, true);
}

function localizeWidgetLabels(node, nodeData) {
    const labels = text({ node, nodeData }).widgets ?? {};
    for (const widget of node.widgets ?? []) {
        const label = labels[widget.name];
        if (!label) {
            continue;
        }
        try {
            widget.label = label;
        } catch (_) {}
        try {
            Object.defineProperty(widget, "displayName", {
                configurable: true,
                get() {
                    return label;
                },
            });
        } catch (_) {}
        try {
            widget.options = widget.options ?? {};
            widget.options.label = label;
        } catch (_) {}
    }
}

function addHelpWidget(node, nodeData) {
    if (node.__surHelpAdded || !node.addDOMWidget) {
        return;
    }
    node.__surHelpAdded = true;
    const labels = text({ node, nodeData });
    const el = document.createElement("div");
    el.style.cssText = [
        "box-sizing:border-box",
        "width:100%",
        "padding:8px 10px",
        "border:1px solid rgba(255,255,255,0.12)",
        "border-radius:8px",
        "background:rgba(255,255,255,0.045)",
        "color:rgba(255,255,255,0.78)",
        "font:12px/1.45 sans-serif",
        "white-space:normal",
    ].join(";");

    const title = document.createElement("div");
    title.textContent = labels.helpTitle;
    title.style.cssText = "font-weight:600;color:rgba(255,255,255,0.92);margin-bottom:4px;";
    el.appendChild(title);

    for (const line of labels.helpLines) {
        const item = document.createElement("div");
        item.textContent = line;
        item.style.marginTop = "3px";
        el.appendChild(item);
    }

    const helpWidget = node.addDOMWidget("sur_stream_help", "SUR help", el, {
        serialize: false,
        hideOnZoom: false,
        getValue() {
            return "";
        },
        setValue() {},
    });
    helpWidget.computeSize = function (width) {
        return [width, 78];
    };
}

function addVideoPanel(node, nodeData) {
    if (node.__surVideoPanelAdded || !node.addDOMWidget) {
        return null;
    }
    const pathWidget = node.widgets?.find((w) => w.name === "video_path");
    if (!pathWidget) {
        return null;
    }
    node.__surVideoPanelAdded = true;

    const labels = text({ node, nodeData });
    const state = {
        videos: [],
        hasPreview: false,
        refresh: null,
        updatePreview: null,
    };

    const root = document.createElement("div");
    root.style.cssText = [
        "box-sizing:border-box",
        "width:100%",
        "display:flex",
        "flex-direction:column",
        "gap:6px",
        "padding:8px",
        "border:1px solid rgba(255,255,255,0.12)",
        "border-radius:8px",
        "background:rgba(0,0,0,0.18)",
    ].join(";");

    const row = document.createElement("div");
    row.style.cssText = "display:grid;grid-template-columns:1fr auto;gap:6px;align-items:center;";

    const select = document.createElement("select");
    select.style.cssText = [
        "min-width:0",
        "height:26px",
        "border-radius:5px",
        "border:1px solid rgba(255,255,255,0.18)",
        "background:#242424",
        "color:rgba(255,255,255,0.9)",
        "font:12px sans-serif",
        "padding:2px 6px",
    ].join(";");

    const refresh = document.createElement("button");
    refresh.type = "button";
    refresh.textContent = labels.refresh;
    refresh.style.cssText = [
        "height:26px",
        "border-radius:5px",
        "border:1px solid rgba(255,255,255,0.18)",
        "background:#303030",
        "color:rgba(255,255,255,0.9)",
        "font:12px sans-serif",
        "padding:0 8px",
        "cursor:pointer",
    ].join(";");

    row.append(select, refresh);
    root.appendChild(row);

    const video = document.createElement("video");
    video.controls = true;
    video.muted = true;
    video.preload = "metadata";
    video.style.cssText = [
        "display:none",
        "width:100%",
        "max-height:180px",
        "border-radius:6px",
        "background:#111",
        "object-fit:contain",
    ].join(";");
    root.appendChild(video);

    const hint = document.createElement("div");
    hint.style.cssText = "font:11px/1.35 sans-serif;color:rgba(255,255,255,0.62);white-space:normal;";
    root.appendChild(hint);

    function fillSelect() {
        select.innerHTML = "";
        const placeholder = document.createElement("option");
        placeholder.value = "";
        placeholder.textContent = state.videos.length ? labels.selectPlaceholder : labels.noVideos;
        select.appendChild(placeholder);

        for (const item of state.videos) {
            const option = document.createElement("option");
            option.value = mediaValue(item);
            option.textContent = `${mediaValue(item)} [${item.type || "input"}, ${formatBytes(item.size)}]`;
            select.appendChild(option);
        }

        const current = String(pathWidget.value || "");
        select.value = [...select.options].some((option) => option.value === current) ? current : "";
    }

    state.updatePreview = function (value = pathWidget.value) {
        const raw = String(value || "").trim();
        const src = viewUrlForValue(raw, state.videos);
        if (src) {
            video.src = src;
            video.style.display = "block";
            hint.textContent = raw;
            state.hasPreview = true;
        } else {
            video.removeAttribute("src");
            video.style.display = "none";
            hint.textContent = raw ? labels.previewUnavailable : "";
            state.hasPreview = false;
        }
        fillSelect();
        markDirty(node);
    };

    state.refresh = async function () {
        try {
            state.videos = await fetchVideos();
        } catch (error) {
            console.warn("[SUR] failed to list videos", error);
            state.videos = [];
        }
        fillSelect();
        state.updatePreview(pathWidget.value);
    };

    select.onchange = () => {
        if (!select.value) {
            return;
        }
        setWidgetValue(pathWidget, select.value);
        state.updatePreview(select.value);
    };
    refresh.onclick = () => state.refresh();
    chainCallback(pathWidget, "callback", () => state.updatePreview(pathWidget.value));

    const panelWidget = node.addDOMWidget("sur_video_panel", "SUR video", root, {
        serialize: false,
        hideOnZoom: false,
        getValue() {
            return "";
        },
        setValue() {},
    });
    panelWidget.computeSize = function (width) {
        return [width, state.hasPreview ? 235 : 70];
    };

    setTimeout(() => state.refresh(), 0);
    node.setSize?.([Math.max(node.size?.[0] ?? 0, 420), node.size?.[1] ?? 0]);
    return state;
}

function addUploadButton(node, nodeData, videoPanel) {
    const labels = text({ node, nodeData });
    const pathWidget = node.widgets?.find((w) => w.name === "video_path");
    if (!pathWidget || node.__surUploadAdded) {
        return;
    }
    node.__surUploadAdded = true;

    const fileInput = document.createElement("input");
    Object.assign(fileInput, {
        type: "file",
        accept: "video/webm,video/mp4,video/x-matroska,video/quicktime,video/x-msvideo,image/gif",
        style: "display: none",
        onchange: async () => {
            if (!fileInput.files?.length) {
                return;
            }
            const resp = await uploadVideo(fileInput.files[0]);
            if (!resp || !resp.ok) {
                alert(`${labels.uploadFailed}: ${resp?.status ?? ""} ${resp?.statusText ?? ""}`);
                return;
            }
            const data = await resp.json();
            if (!data?.ok) {
                alert(`${labels.uploadFailed}: ${data?.error ?? ""}`);
                return;
            }
            const value = data.relative || data.name;
            setWidgetValue(pathWidget, value);
            await videoPanel?.refresh?.();
            videoPanel?.updatePreview?.(value);
            markDirty(node);
            fileInput.value = "";
        },
    });
    document.body.append(fileInput);
    chainCallback(node, "onRemoved", () => fileInput.remove());

    const uploadWidget = node.addWidget("button", labels.upload, "video", () => {
        app.canvas.node_widget = null;
        fileInput.click();
    });
    uploadWidget.options.serialize = false;
}

app.registerExtension({
    name: "Comfyui-Segment-Upscale-Runner.StreamUI",
    beforeRegisterNodeDef(nodeType, nodeData) {
        if (nodeData?.name !== "SegmentVSRFIStreamRunner") {
            return;
        }
        chainCallback(nodeType.prototype, "onNodeCreated", function () {
            localizeWidgetLabels(this, nodeData);
            addHelpWidget(this, nodeData);
            const panel = addVideoPanel(this, nodeData);
            addUploadButton(this, nodeData, panel);
        });
    },
});
