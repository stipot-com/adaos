import { Injectable } from '@angular/core'
import * as Y from 'yjs'
import { IndexeddbPersistence } from 'y-indexeddb'

@Injectable({ providedIn: 'root' })
export class YDocService {
  public readonly doc = new Y.Doc()
  private readonly db = new IndexeddbPersistence('adaos-mobile', this.doc)
  private initialized = false

  async initFromSeedIfEmpty(): Promise<void> {
    if (this.initialized) return
    await this.db.whenSynced
    if (this.doc.share.size === 0) {
      const seed = await fetch('assets/seed.json').then(r => r.json())
      this.doc.transact(() => {
        const ui = this.doc.getMap('ui')
        const data = this.doc.getMap('data')
        ui.set('application', seed.ui.application)
        data.set('weather', seed.data.weather)
      })
    }
    this.initialized = true
  }

  getPath(path: string): any {
    const segs = path.split('/').filter(Boolean)
    let cur: any = this.doc.getMap(segs.shift()!)
    for (const s of segs) {
      if (cur instanceof Y.Map) cur = cur.get(s)
      else if (cur && typeof cur === 'object') cur = cur[s]
      else return undefined
    }
    return cur
  }

  toJSON(val: any): any {
    try {
      const anyVal: any = val
      if (anyVal && typeof anyVal.toJSON === 'function') return anyVal.toJSON()
    } catch {}
    return val
  }
}
