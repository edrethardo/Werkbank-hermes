import assert from 'node:assert/strict'
import { describe, it } from 'vitest'

import { imagePaths, mediaPaths } from '../lib/transcriptImages.js'

describe('transcriptImages', () => {
  it('pulls a plain MEDIA tag', () => {
    assert.deepEqual(mediaPaths('Fertig. MEDIA:/tmp/shot.png'), ['/tmp/shot.png'])
  })

  it('tolerates the markdown wrappers models actually emit', () => {
    // Modelle setzen den Tag routinemäßig in Backticks oder Anführungszeichen; ohne
    // Toleranz wäre die Auslieferung von der Laune des Modells abhängig.
    const text = [
      '`MEDIA:/a.png`',
      '"MEDIA:/b.png"',
      "'MEDIA:/c.png'",
      'MEDIA: /d.png'
    ].join('\n')

    assert.deepEqual(mediaPaths(text), ['/a.png', '/b.png', '/c.png', '/d.png'])
  })

  it('drops sentence punctuation that is not part of the path', () => {
    // "Siehe MEDIA:/tmp/a.png." darf nicht zu "/tmp/a.png." werden — die Datei hieße sonst
    // anders als die, die existiert.
    assert.deepEqual(mediaPaths('Siehe MEDIA:/tmp/a.png.'), ['/tmp/a.png'])
  })

  it('keeps each path once even when repeated', () => {
    assert.deepEqual(mediaPaths('MEDIA:/x.png und nochmal MEDIA:/x.png'), ['/x.png'])
  })

  it('preserves order across several tags', () => {
    assert.deepEqual(mediaPaths('MEDIA:/1.png MEDIA:/2.png MEDIA:/3.png'), ['/1.png', '/2.png', '/3.png'])
  })

  it('separates images from other files', () => {
    // Nicht-Bilder bleiben als Pfad im Text: ein PDF lässt sich nicht rendern, und der
    // Pfad ist brauchbarer als ein stiller Fehlschlag.
    const text = 'MEDIA:/a.png MEDIA:/b.pdf MEDIA:/c.JPEG MEDIA:/d.txt'

    assert.deepEqual(mediaPaths(text).length, 4)
    assert.deepEqual(imagePaths(text), ['/a.png', '/c.JPEG'])
  })

  it('returns nothing for text without tags', () => {
    assert.deepEqual(mediaPaths('Kein Anhang hier, nur /tmp/shot.png als Pfad.'), [])
  })
})
