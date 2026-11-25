# 第三方组件与数据来源

本文件记录随项目使用的第三方组件和服务，不为项目自有代码授予开源许可。Python 依赖以各自上游许可证为准，版本见 [requirements.txt](faretrace/requirements.txt)。

| 组件 / 服务 | 使用方式 | 来源与许可说明 |
| --- | --- | --- |
| Leaflet 1.9.4 | 本地提供 JS、CSS 与默认图像 | [上游](https://github.com/Leaflet/Leaflet/tree/v1.9.4)；BSD 2-Clause，许可见 [vendor/leaflet/LICENSE](faretrace/app/static/vendor/leaflet/LICENSE)；源文件版权头保留 |
| OpenStreetMap | 地图瓦片、地名与近似位置参考 | [版权与 ODbL 说明](https://www.openstreetmap.org/copyright)；地图展示需保留贡献者归属；公开瓦片受[使用政策](https://operations.osmfoundation.org/policies/tiles/)约束 |
| OSRM 公共演示服务 | 可选路线准备脚本 | [OSRM](https://project-osrm.org/)；公共演示服务不提供生产可用性保证；使用的数据仍受其来源许可约束 |
| Nominatim 公共服务 | 可选地理编码与本地缓存 | [使用政策](https://operations.osmfoundation.org/policies/nominatim/)；脚本限速并缓存，实际使用仍需遵守服务政策 |

`faretrace/data/` 中路线 / 地理编码缓存用于演示，不能视为独立拥有版权的路网数据集。订单、金额与轨迹为合成数据，见 [ASSUMPTIONS.md](faretrace/ASSUMPTIONS.md)。如有额外素材或数据来源，应在重新发布前补齐来源与授权说明。
