#!/usr/bin/env node

import { spawnSync } from 'node:child_process'
import { createReadStream, mkdtempSync, rmSync, statSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { basename, extname, join } from 'node:path'
import process from 'node:process'
import { HeadObjectCommand, PutObjectCommand, S3Client } from '@aws-sdk/client-s3'

function fail(message) {
  console.error(message)
  process.exit(1)
}

function readFlag(args, ...names) {
  for (let index = 0; index < args.length; index += 1) {
    const value = args[index]
    if (!names.includes(value)) continue
    return args[index + 1]
  }
  return undefined
}

function slugify(value) {
  return value
    .toLowerCase()
    .trim()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '')
}

function normalizeBaseUrl(value) {
  return value.replace(/\/+$/, '')
}

function isAbsoluteUrl(value) {
  return /^[a-z]+:\/\//i.test(value)
}

function normalizeUploadName(value) {
  const stem = slugify(basename(value, extname(value)))
  return stem || 'upload'
}

function normalizePrefix(value) {
  return value.replace(/^\/+|\/+$/g, '')
}

function contentTypeForExtension(extension) {
  const types = {
    '.aac': 'audio/aac',
    '.gif': 'image/gif',
    '.jpeg': 'image/jpeg',
    '.jpg': 'image/jpeg',
    '.json': 'application/json',
    '.m4a': 'audio/mp4',
    '.mov': 'video/quicktime',
    '.mp3': 'audio/mpeg',
    '.mp4': 'video/mp4',
    '.ogg': 'audio/ogg',
    '.pdf': 'application/pdf',
    '.png': 'image/png',
    '.svg': 'image/svg+xml',
    '.txt': 'text/plain; charset=utf-8',
    '.wav': 'audio/wav',
    '.webm': 'video/webm',
    '.webp': 'image/webp',
  }
  return types[extension] || 'application/octet-stream'
}

function transcodeMovToMp4(filePath) {
  const tempDir = mkdtempSync(join(tmpdir(), 'telegram-bridge-upload-'))
  const outputPath = join(tempDir, 'upload.mp4')
  const result = spawnSync(
    'ffmpeg',
    [
      '-y',
      '-i',
      filePath,
      '-map',
      '0:v:0',
      '-map',
      '0:a?',
      '-vf',
      'scale=trunc(iw/2)*2:trunc(ih/2)*2',
      '-c:v',
      'libx264',
      '-preset',
      'medium',
      '-crf',
      '23',
      '-pix_fmt',
      'yuv420p',
      '-c:a',
      'aac',
      '-b:a',
      '192k',
      '-movflags',
      '+faststart',
      outputPath,
    ],
    { stdio: 'inherit' }
  )

  if (result.error) {
    rmSync(tempDir, { recursive: true, force: true })
    fail(`Failed to start ffmpeg for MOV conversion: ${result.error.message}`)
  }

  if (result.status != 0) {
    rmSync(tempDir, { recursive: true, force: true })
    fail('ffmpeg failed while converting the MOV input to MP4.')
  }

  return {
    uploadPath: outputPath,
    cleanup: () => rmSync(tempDir, { recursive: true, force: true }),
    transcodedFrom: '.mov',
    finalExtension: '.mp4',
    contentType: 'video/mp4',
  }
}

function prepareUploadSource(filePath) {
  const extension = extname(filePath).toLowerCase()

  if (extension === '.mov') {
    return transcodeMovToMp4(filePath)
  }

  return {
    uploadPath: filePath,
    cleanup: () => {},
    transcodedFrom: null,
    finalExtension: extension,
    contentType: contentTypeForExtension(extension),
  }
}

function isMissingObjectError(error) {
  return (
    error &&
    typeof error === 'object' &&
    (error.name === 'NotFound' ||
      error.Code === 'NotFound' ||
      error.code === 'NotFound' ||
      error?.$metadata?.httpStatusCode === 404)
  )
}

async function assertObjectKeyAvailable(client, bucket, objectKey) {
  try {
    await client.send(new HeadObjectCommand({ Bucket: bucket, Key: objectKey }))
  } catch (error) {
    if (isMissingObjectError(error)) {
      return
    }
    throw error
  }

  fail(`Naming conflict: ${objectKey} already exists.`)
}

const args = process.argv.slice(2)
const filePath = readFlag(args, '--file', '-f')
const uploadName = normalizeUploadName(readFlag(args, '--name', '-n') || '')

if (!filePath) {
  fail('Missing required argument: --file <path-to-media>')
}

if (!uploadName) {
  fail('Missing required argument: --name <upload-name>')
}

const endpoint = process.env.OBJECT_STORAGE_ENDPOINT?.trim()
const bucket = process.env.OBJECT_STORAGE_BUCKET?.trim()
const accessKeyId = process.env.OBJECT_STORAGE_ACCESS_KEY_ID?.trim()
const secretAccessKey = process.env.OBJECT_STORAGE_SECRET_ACCESS_KEY?.trim()
const region = process.env.OBJECT_STORAGE_REGION?.trim() || 'us-east-1'
const prefix = normalizePrefix(process.env.OBJECT_STORAGE_PREFIX?.trim() || 'uploads')

if (!endpoint || !bucket || !accessKeyId || !secretAccessKey) {
  fail(
    'Missing object storage configuration. Set OBJECT_STORAGE_ENDPOINT, OBJECT_STORAGE_BUCKET, OBJECT_STORAGE_ACCESS_KEY_ID, and OBJECT_STORAGE_SECRET_ACCESS_KEY.'
  )
}

const normalizedEndpoint = isAbsoluteUrl(endpoint) ? endpoint : `https://${endpoint}`
const publicBaseUrl = normalizeBaseUrl(
  process.env.OBJECT_STORAGE_PUBLIC_BASE_URL?.trim() || `https://${bucket}.${normalizedEndpoint.replace(/^https?:\/\//, '')}`
)

const uploadSource = prepareUploadSource(filePath)
const suffix = uploadSource.finalExtension
const objectKey = prefix ? `${prefix}/${uploadName}${suffix}` : `${uploadName}${suffix}`
const stats = statSync(uploadSource.uploadPath)

const client = new S3Client({
  region,
  endpoint: normalizedEndpoint,
  forcePathStyle: false,
  credentials: {
    accessKeyId,
    secretAccessKey,
  },
})

try {
  await assertObjectKeyAvailable(client, bucket, objectKey)

  const response = await client.send(
    new PutObjectCommand({
      Bucket: bucket,
      Key: objectKey,
      Body: createReadStream(uploadSource.uploadPath),
      ContentType: uploadSource.contentType,
      CacheControl: 'public, max-age=31536000, immutable',
    })
  )

  console.log(
    JSON.stringify(
      {
        bucket,
        key: objectKey,
        url: `${publicBaseUrl}/${objectKey}`,
        sizeBytes: stats.size,
        contentType: uploadSource.contentType,
        transcodedFrom: uploadSource.transcodedFrom,
        etag: response.ETag || null,
      },
      null,
      2
    )
  )
} catch (error) {
  fail(error instanceof Error ? error.message : String(error))
} finally {
  uploadSource.cleanup()
}