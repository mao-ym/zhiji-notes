const MAX_ATTACHMENT_BYTES = 2 * 1024 * 1024;
const TARGET_ATTACHMENT_BYTES = Math.floor(1.8 * 1024 * 1024);
const MAX_IMAGE_DIMENSION = 2048;
const SUPPORTED_IMAGE_TYPES = new Set([
  "image/png",
  "image/jpeg",
  "image/gif",
  "image/webp",
]);

function dataUrlByteLength(dataUrl) {
  const comma = dataUrl.indexOf(",");
  if (comma < 0) return 0;
  const base64 = dataUrl.slice(comma + 1);
  const padding = base64.endsWith("==") ? 2 : base64.endsWith("=") ? 1 : 0;
  return Math.floor((base64.length * 3) / 4) - padding;
}

function fileToDataUrl(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result));
    reader.onerror = () => reject(new Error("无法读取图片"));
    reader.readAsDataURL(file);
  });
}

function loadImage(dataUrl) {
  return new Promise((resolve, reject) => {
    const image = new Image();
    image.onload = () => resolve(image);
    image.onerror = () => reject(new Error("无法解码图片"));
    image.src = dataUrl;
  });
}

function outputFilename(filename, mimeType) {
  const stem = filename.replace(/\.[^.]+$/, "") || "image";
  return `${stem}.${mimeType === "image/webp" ? "webp" : "jpg"}`;
}

function encodeCanvas(canvas, mimeType, quality) {
  const dataUrl = canvas.toDataURL(mimeType, quality);
  if (!dataUrl.startsWith(`data:${mimeType};base64,`)) return null;
  return dataUrl;
}

function compressImage(image, filename) {
  const scale = Math.min(
    1,
    MAX_IMAGE_DIMENSION / Math.max(image.naturalWidth, image.naturalHeight),
  );
  const canvas = document.createElement("canvas");
  canvas.width = Math.max(1, Math.round(image.naturalWidth * scale));
  canvas.height = Math.max(1, Math.round(image.naturalHeight * scale));
  const context = canvas.getContext("2d");
  if (!context) throw new Error("无法压缩图片");
  context.drawImage(image, 0, 0, canvas.width, canvas.height);

  let smallest = null;
  for (const mimeType of ["image/webp", "image/jpeg"]) {
    for (const quality of [0.85, 0.75, 0.65, 0.55, 0.45, 0.35]) {
      const dataUrl = encodeCanvas(canvas, mimeType, quality);
      if (!dataUrl) continue;
      const byteLength = dataUrlByteLength(dataUrl);
      const candidate = { data_url: dataUrl, mime_type: mimeType, byte_length: byteLength };
      if (!smallest || byteLength < smallest.byte_length) {
        smallest = candidate;
      }
      if (byteLength <= TARGET_ATTACHMENT_BYTES) {
        return {
          filename: outputFilename(filename, mimeType),
          ...candidate,
        };
      }
    }
  }
  if (smallest && smallest.byte_length <= MAX_ATTACHMENT_BYTES) {
    return { filename: outputFilename(filename, smallest.mime_type), ...smallest };
  }
  throw new Error("图片压缩后仍超过 2 MiB");
}

export async function readImageFile(file) {
  if (!file || !SUPPORTED_IMAGE_TYPES.has(file.type)) {
    throw new Error("仅支持 PNG、JPEG、GIF 或 WebP 图片");
  }
  const originalDataUrl = await fileToDataUrl(file);
  if (file.size <= MAX_ATTACHMENT_BYTES) {
    return {
      filename: file.name,
      mime_type: file.type,
      byte_length: file.size,
      data_url: originalDataUrl,
    };
  }
  const image = await loadImage(originalDataUrl);
  return compressImage(image, file.name);
}
