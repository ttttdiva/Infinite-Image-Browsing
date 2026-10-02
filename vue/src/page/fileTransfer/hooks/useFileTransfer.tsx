import { watch, ref } from 'vue'
import {
  useWatchDocument
} from 'vue3-ts-util'
import { FileTransferData, getFileTransferDataFromDragEvent, isImageFile } from '@/util/file'
import type { FileNodeInfo } from '@/api/files'
import { useHookShareState, global, events, sli } from '.'
import { copyFiles, moveFiles, uploadImage } from '@/api/files'
import { MultiSelectTips } from '@/components/functionalCallableComp'
import { t } from '@/i18n'
import { createReactiveQueue } from '@/util'
import { Modal, Button, Checkbox, message } from 'ant-design-vue'
import * as Path from '@/util/path'

import { cloneDeep, uniqBy } from 'lodash-es'

export function useFileTransfer () {
  const { currLocation, sortedFiles, currPage, multiSelectedIdxs, eventEmitter, walker } =
    useHookShareState().toRefs()
  const recover = () => {
    multiSelectedIdxs.value = []
  }
  useWatchDocument('click', () => {
    if (!global.keepMultiSelect) {
      recover()
    }
  })
  useWatchDocument('blur', () => {
    if (!global.keepMultiSelect) {
      recover()
    }
  })
  watch(currPage, recover)

  const onFileDragStart = (e: DragEvent, idx: number) => {
    const file = cloneDeep(sortedFiles.value[idx])
    sli.fileDragging = true
    console.log('onFileDragStart set drag file ', e, idx, file)
    const files = [file]
    let includeDir = file.type === 'dir'
    if (multiSelectedIdxs.value.includes(idx)) {
      const selectedFiles = multiSelectedIdxs.value.map((idx) => sortedFiles.value[idx])
      files.push(...selectedFiles)
      includeDir = selectedFiles.some((v) => v.type === 'dir')
    }
    const data: FileTransferData = {
      includeDir,
      loc: currLocation.value || 'search-result',
      path: uniqBy(files, 'fullpath').map((f) => f.fullpath),
      nodes: uniqBy(files, 'fullpath'),
      __id: 'FileTransferData'
    }
    e.dataTransfer!.setData('text/plain', JSON.stringify(data))
  }

  const onFileDragEnd = () => {
    sli.fileDragging = false
  }

  const uploadDroppedImages = async (e: DragEvent, toPath: string) => {
    const droppedFiles = Array.from(e.dataTransfer?.files ?? [])
    if (!droppedFiles.length) {
      return false
    }

    e.preventDefault()
    e.stopPropagation()

    const images = droppedFiles.filter((file) => isImageFile(file.name))
    if (!images.length) {
      message.warning(t('dragDropImagesOnly'))
      return true
    }

    const key = 'drag-drop-image-import'
    message.loading({
      content: t('importingDroppedImages', { count: images.length }),
      key,
      duration: 0
    })

    let success = 0
    const failed: string[] = []
    for (const file of images) {
      try {
        await uploadImage(file, toPath)
        success += 1
      } catch (error: any) {
        console.error('Failed to import dropped image', file.name, error)
        failed.push(file.name)
      }
    }

    message.destroy(key)
    eventEmitter.value.emit('refresh')

    if (failed.length) {
      message.error(t('importDroppedImagesFailed', { success, failed: failed.length }))
    } else {
      message.success(t('importedDroppedImages', { count: success }))
    }
    return true
  }
  const onDrop = async (e: DragEvent) => {
    if (walker.value) {
      return
    }
    const data = getFileTransferDataFromDragEvent(e)
    const toPath = currLocation.value
    if (!data) {
      await uploadDroppedImages(e, toPath)
      return
    }
    if (data.loc === toPath) {
      return
    }
    openMoveOrCopyConfirm(data, toPath)
  }

  const onFileDropToFolder = async (e: DragEvent, target: FileNodeInfo) => {
    if (walker.value || target.type !== 'dir') {
      return false
    }
    const data = getFileTransferDataFromDragEvent(e)
    if (!data) {
      return uploadDroppedImages(e, target.fullpath)
    }
    const toPath = Path.normalize(target.fullpath)
    const filtered = data.path
      .map(Path.normalize)
      .filter((p) => p !== toPath && !toPath.startsWith(p + '/'))
    if (!filtered.length) {
      return false
    }
    e.preventDefault()
    openMoveOrCopyConfirm({ ...data, path: filtered }, toPath)
    return true
  }

  const openMoveOrCopyConfirm = (data: FileTransferData, toPath: string) => {
    const q = createReactiveQueue()
    const continueOnError = ref(false)
    const onCopyBtnClick = async () => q.pushAction(async () => {
      await copyFiles(data.path, toPath, false, continueOnError.value)
      eventEmitter.value.emit('refresh')
      Modal.destroyAll()
    })

    const onMoveBtnClick = () => q.pushAction(async () => {
      await moveFiles(data.path, toPath, false, continueOnError.value)
      events.emit('removeFiles', { paths: data.path, loc: data.loc })
      eventEmitter.value.emit('refresh')
      Modal.destroyAll()
    })
    Modal.confirm({
      title: t('confirm') + '?',
      width: '60vw',
      content: () => <div>
        <div>
          {`${t('moveSelectedFilesTo')} ${toPath}`}
          <ol style={{ maxHeight: '50vh', overflow: 'auto' }}>
            {data.path.map((v) => <li>{v.split(/[/\\]/).pop()}</li>)}
          </ol>
        </div>
        <MultiSelectTips />
        <div style={{ marginTop: '8px' }}>
          <Checkbox v-model:checked={continueOnError.value}>{t('continueOnError')}</Checkbox>
          <div style={{ color: '#888', fontSize: '12px', marginTop: '4px' }}>{t('continueOnErrorDesc')}</div>
        </div>
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'flex-end' }} class="actions">
          <Button onClick={Modal.destroyAll}>{t('cancel')}</Button>
          <Button type="primary" loading={!q.isIdle} onClick={onCopyBtnClick}>{t('copy')}</Button>
          <Button type="primary" loading={!q.isIdle} onClick={onMoveBtnClick}>{t('move')}</Button>
        </div>
      </div>,
      maskClosable: true,
      wrapClassName: 'hidden-antd-btns-modal'
    })
  }
  return {
    onFileDragStart,
    onDrop,
    multiSelectedIdxs,
    onFileDragEnd,
    onFileDropToFolder
  }
}
