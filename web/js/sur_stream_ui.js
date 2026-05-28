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

function currentLocale() {
    const language = (navigator.language || "").toLowerCase();
    if (language.startsWith("zh")) {
        return "zh";
    }
    if (language.startsWith("ja")) {
        return "ja";
    }
    return "en";
}

const I18N = {
    zh: {
        upload: "选择/上传视频",
        uploadFailed: "视频上传失败",
        helpTitle: "SUR 流式处理",
        helpLines: [
            "使用 FlashVSR-v1.1 做放大，按小块直接读写视频，避免把整段变成 ComfyUI 大张量。",
            "video_path 可填 input 下文件名；点“选择/上传视频”会自动填入。",
            "output_path 留空会保存到 output/VSRFI；推荐先用 scale=2、插帧=2、chunk=21、桥接帧=1。"
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
        helpTitle: "SUR Stream",
        helpLines: [
            "FlashVSR-v1.1 を使い、小さなチャンク単位で動画を直接読み書きします。",
            "video_path には input 内のファイル名を指定できます。ボタンで選択すると自動入力されます。",
            "output_path を空にすると output/VSRFI に保存されます。まずは scale=2、interpolation=2、chunk=21、bridge=1 を推奨します。"
        ],
        widgets: {
            video_path: "動画ファイル",
            output_path: "出力パス",
            execute: "実行",
            scale: "拡大倍率",
            interpolation_factor: "補間倍率",
            vfi_method: "補間方式",
            frames_per_chunk: "chunk フレーム",
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
        helpTitle: "SUR Stream",
        helpLines: [
            "Uses FlashVSR-v1.1 and streams small chunks directly from video to video instead of building huge ComfyUI tensors.",
            "video_path can be a filename under input; the upload button fills it automatically.",
            "Leave output_path blank for output/VSRFI. Good first settings: scale=2, interpolation=2, chunk=21, bridge=1."
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

function text() {
    return I18N[currentLocale()] ?? I18N.en;
}

async function uploadVideo(file) {
    const body = new FormData();
    const safeFile = new File([file], file.name, {
        type: file.type,
        lastModified: file.lastModified,
    });
    body.append("image", safeFile);
    return await api.fetchApi("/upload/image", {
        method: "POST",
        body,
    });
}

function setWidgetValue(widget, value) {
    widget.value = value;
    if (widget.options?.values && !widget.options.values.includes(value)) {
        widget.options.values.push(value);
    }
    widget.callback?.(value);
}

function moveWidgetAfter(node, widget, afterName) {
    if (!node.widgets || !widget) {
        return;
    }
    const from = node.widgets.indexOf(widget);
    const after = node.widgets.findIndex((w) => w.name === afterName);
    if (from < 0 || after < 0 || from === after + 1) {
        return;
    }
    node.widgets.splice(from, 1);
    const newAfter = node.widgets.findIndex((w) => w.name === afterName);
    node.widgets.splice(newAfter + 1, 0, widget);
}

function addUploadButton(node) {
    const labels = text();
    const pathWidget = node.widgets?.find((w) => w.name === "video_path");
    if (!pathWidget || node.__surUploadAdded) {
        return;
    }
    node.__surUploadAdded = true;

    const fileInput = document.createElement("input");
    Object.assign(fileInput, {
        type: "file",
        accept: "video/webm,video/mp4,video/x-matroska,video/quicktime,image/gif",
        style: "display: none",
        onchange: async () => {
            if (!fileInput.files?.length) {
                return;
            }
            const resp = await uploadVideo(fileInput.files[0]);
            if (!resp || resp.status !== 200) {
                alert(`${labels.uploadFailed}: ${resp?.status ?? ""} ${resp?.statusText ?? ""}`);
                return;
            }
            const data = await resp.json();
            setWidgetValue(pathWidget, data.name);
            app.canvas.setDirty?.(true, true);
            app.graph.setDirtyCanvas?.(true, true);
        },
    });
    document.body.append(fileInput);
    chainCallback(node, "onRemoved", () => fileInput.remove());

    const uploadWidget = node.addWidget("button", labels.upload, "video", () => {
        app.canvas.node_widget = null;
        fileInput.click();
    });
    uploadWidget.options.serialize = false;
    moveWidgetAfter(node, uploadWidget, "video_path");
}

function localizeWidgetLabels(node) {
    const labels = text().widgets ?? {};
    for (const widget of node.widgets ?? []) {
        const label = labels[widget.name];
        if (!label) {
            continue;
        }
        widget.label = label;
        widget.displayName = label;
        widget.options = widget.options ?? {};
        widget.options.label = label;
    }
}

function addHelpWidget(node) {
    if (node.__surHelpAdded || !node.addDOMWidget) {
        return;
    }
    node.__surHelpAdded = true;
    const labels = text();
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
        return [width, currentLocale() === "zh" ? 96 : 106];
    };
    node.setSize?.([Math.max(node.size?.[0] ?? 0, 360), node.size?.[1] ?? 0]);
}

app.registerExtension({
    name: "Comfyui-Segment-Upscale-Runner.StreamUI",
    beforeRegisterNodeDef(nodeType, nodeData) {
        if (nodeData?.name !== "SegmentVSRFIStreamRunner") {
            return;
        }
        chainCallback(nodeType.prototype, "onNodeCreated", function () {
            localizeWidgetLabels(this);
            addHelpWidget(this);
            addUploadButton(this);
        });
    },
});
