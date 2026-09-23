const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { COLORS, values, appearance, criticalIndicators, directionValues } = require('../static/city-map.js');
const config = require('../config.json');
const districts = require('../data/districts.json');
const geojson = JSON.parse(fs.readFileSync(path.join(__dirname, '../static/data/astana-districts.geojson'), 'utf8'));

test('map respects separate district and indicator thresholds, including exact boundaries', () => {
  for (const [value, expected] of [[0,'red'],[39.99,'red'],[40,'red'],[49.99,'red'],[50,'amber'],[60,'amber'],[60.01,'green'],[100,'green']]) {
    assert.equal(appearance({d_before:value,d_after:value}).color, COLORS[expected]);
  }
  for (const [value, expected] of [[39.99,'red'],[40,'amber'],[50,'amber'],[60,'amber'],[60.01,'green']]) {
    assert.equal(appearance({indicators_before:{T1:value},indicators_after:{T1:value}},'T1').color, COLORS[expected]);
  }
});

test('before, after and effect views use the server values without changing scores', () => {
  const dd = {d_before:49.18,d_after:52.96,indicators_before:{S2:35},indicators_after:{S2:43.75}};
  const saved = structuredClone(dd);
  assert.equal(appearance(dd,'D','before').color, COLORS.red);
  assert.equal(appearance(dd,'D','after').color, COLORS.amber);
  assert.deepEqual(values(dd), {before:49.18,after:52.96,delta:3.78});
  assert.equal(appearance(dd,'S2','delta').value, 8.75);
  assert.equal(appearance(dd,'S2','delta').color, COLORS.up);
  assert.equal(appearance({d_before:52,d_after:49},'D','delta').color,COLORS.down);
  assert.equal(appearance({d_before:52,d_after:52},'D','delta').color,COLORS.flat);
  assert.deepEqual(dd,saved);
});

test('missing and non-finite observations never become a real city rating', () => {
  for (const dd of [undefined, {}, {d_before:50}, {d_before:50,d_after:NaN}, {d_before:50,d_after:null}, {d_before:50,d_after:Infinity}]) {
    for (const mode of ['before','after','delta']) {
      assert.equal(appearance(dd,'D',mode).color, COLORS.gray);
      assert.equal(appearance(dd,'D',mode).value, null);
    }
  }
});

test('critical markers follow the displayed time, and exactly 40 is not critical', () => {
  const dd = {indicators_before:{S1:38,S2:35,T1:40},indicators_after:{S1:48,S2:43.75,T1:40}};
  assert.deepEqual(criticalIndicators(dd,'before'),['S1','S2']);
  assert.deepEqual(criticalIndicators(dd,'after'),[]);
  assert.deepEqual(criticalIndicators(dd,'delta'),[]);
  assert.deepEqual(criticalIndicators(undefined),[]);
});

test('direction bars average the matching indicators and respect time selection', () => {
  const dd = {indicators_before:districts.find(d=>d.id==='nura').indicators,indicators_after:{S1:48,S2:43.75}};
  assert.equal(directionValues(dd,config,'before').find(d=>d.id==='social').value,36.5);
  assert.equal(directionValues(dd,config,'after').find(d=>d.id==='social').value,45.875);
  assert.equal(directionValues(dd,config,'after').find(d=>d.id==='transport').value,null);
});

function inRing(point, ring) {
  let result = false;
  const [x,y] = point;
  for(let i=1;i<ring.length;i++) {
    const [a,b] = [ring[i-1],ring[i]];
    if ((a[1]>y)!==(b[1]>y) && x<(b[0]-a[0])*(y-a[1])/(b[1]-a[1])+a[0]) result=!result;
  }
  return result;
}

test('local geographic snapshot covers all five model districts and marks the sixth unscored', () => {
  assert.equal(geojson.type,'FeatureCollection');
  assert.equal(geojson.license,'ODbL-1.0');
  assert.equal(geojson.features.length,6);
  const ids = geojson.features.map(f=>f.properties.id);
  assert.equal(new Set(ids).size,6);
  for (const d of districts) {
    const f = geojson.features.find(f=>f.properties.id===d.id);
    assert.ok(f, d.id);
    assert.equal(f.properties.model_data,true);
    assert.equal(f.properties.name,d.name);
  }
  const extra = geojson.features.filter(f=>!districts.some(d=>d.id===f.properties.id));
  assert.equal(extra.length,1);
  assert.equal(extra[0].properties.model_data,false);
  assert.equal(extra[0].properties.id,'saraishyk');
});

test('real boundaries are closed finite rings in Astana and each label sits inside its district', () => {
  for(const f of geojson.features) {
    assert.ok(Number.isInteger(f.properties.osm_relation));
    assert.ok(Number.isInteger(f.properties.osm_version));
    assert.equal(f.geometry.type,'MultiPolygon');
    for(const polygon of f.geometry.coordinates) {
      for(const ring of polygon) {
        assert.ok(ring.length>=4);
        assert.deepEqual(ring[0],ring[ring.length-1]);
        for(const [lon,lat] of ring) {
          assert.ok(Number.isFinite(lon) && lon>71 && lon<72);
          assert.ok(Number.isFinite(lat) && lat>50.8 && lat<51.5);
        }
      }
    }
    assert.ok(f.geometry.coordinates.some(p=>inRing(f.properties.label,p[0]) && !p.slice(1).some(h=>inRing(f.properties.label,h))),f.properties.id);
  }
});
