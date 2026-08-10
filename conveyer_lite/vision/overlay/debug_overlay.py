from vision.overlay.renderers.primitives       import DrawPrimitives
from vision.overlay.renderers.construction_error import draw_construction_error
from vision.overlay.renderers.window_geometry  import WindowGeometryRenderer
from vision.overlay.renderers.window_sinks     import WindowSinksRenderer
from vision.overlay.renderers.contacts_long    import ContactsLongRenderer
from vision.overlay.renderers.contacts_short   import ContactsShortRenderer
from vision.overlay.renderers.long_omission    import LongOmissionRenderer
from vision.overlay.renderers.short_omission   import ShortOmissionRenderer
from vision.overlay.renderers.top_contacts     import TopContactsRenderer
from vision.overlay.renderers.top_platform     import TopPlatformRenderer
from vision.overlay.renderers.top_sinks        import TopSinksRenderer
from vision.overlay.renderers.top_glass        import TopGlassRenderer
from vision.overlay.renderers.platform_overlap import PlatformOverlapRenderer


class DebugOverlay:
    """
    Фасад рендеринга debug-оверлея.
    """

    @staticmethod
    def render_frame(frame, role, rule_results):
        img = frame.copy()

        role_drawings = []
        for rr in rule_results:
            for d in rr.drawings:
                if d.get("role") == role:
                    role_drawings.append(d)

        available_types = {
            drawing.get("type", "") for drawing in role_drawings
        }
        composed_drawings = []
        construction_messages = set()
        construction_slot = 0
        for original in role_drawings:
            drawing = original
            draw_type = drawing.get("type", "")
            if draw_type == "construction_error":
                message = str(drawing.get("message") or "NO GEOMETRY")
                if message in construction_messages:
                    continue
                construction_messages.add(message)
                drawing = dict(drawing)
                drawing["slot"] = construction_slot
                construction_slot += 1
            elif (
                draw_type == "platform_overlap_platform"
                and "top_platform_actual" in available_types
            ):
                # Более точная отрисовка уже есть в наборе: дубль пропускаем.
                continue
            elif draw_type == "window_sink_overlap":
                drawing = dict(drawing)
                drawing["draw_window_reference"] = (
                    "window_geometry_item" not in available_types
                )
            elif draw_type == "top_sinks_references":
                drawing = dict(drawing)
                drawing["draw_platform_reference"] = (
                    "top_platform_actual" not in available_types
                )
                drawing["draw_contact_references"] = (
                    "top_contacts_item" not in available_types
                )
            elif draw_type == "top_glass_cleanup_references":
                drawing = dict(drawing)
                drawing["draw_platform_reference"] = (
                    "top_platform_actual" not in available_types
                )
                drawing["draw_central_reference"] = (
                    "top_sinks_references" not in available_types
                )
            elif (
                draw_type == "top_glass_bad_references"
                and "top_contacts_item" in available_types
            ):
                continue
            composed_drawings.append(drawing)

        composed_drawings.sort(
            key=lambda drawing: drawing.get("type") == "construction_error"
        )
        for d in composed_drawings:
            draw_type = d.get("type", "")

            if draw_type == "stats_panel_entry":
                # Числовая статистика отображается только в правой UI-панели.
                continue

            # --- Общая короткая ошибка невозможного построения ---
            if draw_type == "construction_error":
                draw_construction_error(img, d)

            # --- Общие bbox ---
            elif draw_type == "rule_bbox":
                DrawPrimitives.draw_rule_bbox(img, d)

            # --- Window geometry ---
            elif draw_type == "window_geometry_item":
                WindowGeometryRenderer.draw_item(img, d)
            elif draw_type == "window_geometry_count_item":
                WindowGeometryRenderer.draw_count_item(img, d)
            elif draw_type == "window_geometry_ignored":
                WindowGeometryRenderer.draw_ignored(img, d)

            # --- Window sinks ---
            elif draw_type == "window_sink_overlap":
                WindowSinksRenderer.draw_overlap(img, d)
            elif draw_type == "window_sink_invalid_reference":
                WindowSinksRenderer.draw_invalid_reference(img, d)
            elif draw_type == "window_sink_reference_count_item":
                WindowSinksRenderer.draw_reference_count_item(img, d)

            # --- Contacts long ---
            elif draw_type == "contacts_long_item":
                ContactsLongRenderer.draw_item(img, d)
            elif draw_type == "contacts_long_count_item":
                ContactsLongRenderer.draw_count_item(img, d)
            elif draw_type == "contacts_long_invalid_mask":
                ContactsLongRenderer.draw_invalid_mask(img, d)
            elif draw_type == "contacts_long_ignored":
                ContactsLongRenderer.draw_ignored(img, d)
            elif draw_type == "contacts_long_fit_line":
                ContactsLongRenderer.draw_fit_line(img, d)
            elif draw_type == "contacts_long_level_center":
                ContactsLongRenderer.draw_level_center(img, d)
            elif draw_type == "contacts_long_omission_line":
                ContactsLongRenderer.draw_omission_line(img, d)
            elif draw_type == "contacts_long_omission_distance":
                ContactsLongRenderer.draw_omission_distance(img, d)
            elif draw_type == "contacts_long_omission_missing":
                ContactsLongRenderer.draw_omission_missing(img, d)
            elif draw_type == "contacts_long_inscribed_rect":
                ContactsLongRenderer.draw_inscribed_rect(img, d)

            # --- Contacts short ---
            elif draw_type == "contacts_short_item":
                ContactsShortRenderer.draw_item(img, d)
            elif draw_type == "contacts_short_count_item":
                ContactsShortRenderer.draw_count_item(img, d)
            elif draw_type == "contacts_short_invalid_mask":
                ContactsShortRenderer.draw_invalid_mask(img, d)
            elif draw_type == "contacts_short_ignored":
                ContactsShortRenderer.draw_ignored(img, d)
            elif draw_type == "contacts_short_level_line":
                ContactsShortRenderer.draw_level_line(img, d)
            elif draw_type == "contacts_short_level_center":
                ContactsShortRenderer.draw_level_center(img, d)
            elif draw_type == "contacts_short_height_segment":
                ContactsShortRenderer.draw_height_segment(img, d)
            elif draw_type == "contacts_short_omission_line":
                ContactsShortRenderer.draw_omission_line(img, d)
            elif draw_type == "contacts_short_omission_distance":
                ContactsShortRenderer.draw_omission_distance(img, d)
            elif draw_type == "contacts_short_omission_missing":
                ContactsShortRenderer.draw_omission_missing(img, d)
            elif draw_type == "contacts_short_inscribed_rect":
                ContactsShortRenderer.draw_inscribed_rect(img, d)

            # --- Long omission: устойчивая толщина объединённой mask ---
            elif draw_type == "long_omission_item":
                LongOmissionRenderer.draw_item(img, d)

            # --- Short omission: устойчивая толщина объединённой mask ---
            elif draw_type == "short_omission_item":
                ShortOmissionRenderer.draw_item(img, d)

            # --- Top contacts ---
            elif draw_type == "top_contacts_platform_bbox":
                TopContactsRenderer.draw_platform_bbox(img, d)
            elif draw_type == "top_contacts_group_reference":
                TopContactsRenderer.draw_group_reference(img, d)
            elif draw_type == "top_contacts_distance":
                TopContactsRenderer.draw_distance(img, d)
            elif draw_type == "top_contacts_item":
                TopContactsRenderer.draw_item(img, d)
            elif draw_type == "top_contacts_count_item":
                TopContactsRenderer.draw_count_item(img, d)
            elif draw_type == "top_contacts_invalid_mask":
                TopContactsRenderer.draw_invalid_mask(img, d)
            elif draw_type == "top_contacts_ignored":
                TopContactsRenderer.draw_ignored(img, d)
            elif draw_type == "top_contact_inscribed_rect":
                TopContactsRenderer.draw_inscribed_rect(img, d)

            # --- Top platform ---
            elif draw_type == "top_platform_actual":
                TopPlatformRenderer.draw_actual(img, d)
            elif draw_type == "top_platform_inscribed_rect":
                TopPlatformRenderer.draw_inscribed_rect(img, d)
            elif draw_type == "top_platform_centers":
                TopPlatformRenderer.draw_centers(img, d)

            # --- Top sinks ---
            elif draw_type == "top_sinks_references":
                TopSinksRenderer.draw_references(img, d)
            elif draw_type == "top_sink_forbidden_region":
                TopSinksRenderer.draw_forbidden_region(img, d)
            elif draw_type == "top_sink_invalid_reference":
                TopSinksRenderer.draw_invalid_reference(img, d)
            elif draw_type == "top_sink_reference_contact":
                TopSinksRenderer.draw_reference_contact(img, d)

            # --- Top glass (cleanup zones -> CLEANUP) ---
            elif draw_type == "top_glass_cleanup_references":
                TopGlassRenderer.draw_cleanup_references(img, d)
            elif draw_type == "top_glass_cleanup_region":
                TopGlassRenderer.draw_cleanup_region(img, d)

            # --- Top glass on contacts (-> BAD) ---
            elif draw_type == "top_glass_bad_references":
                TopGlassRenderer.draw_bad_references(img, d)
            elif draw_type == "top_glass_contact_overlap":
                TopGlassRenderer.draw_contact_overlap(img, d)
            elif draw_type == "top_glass_bad_glass":
                TopGlassRenderer.draw_bad_glass(img, d)

            # --- Platform overlap rule: outer overflow boundary ---
            elif draw_type == "platform_overlap_platform":
                PlatformOverlapRenderer.draw_platform(img, d)
            elif draw_type == "platform_overlap_boundary":
                PlatformOverlapRenderer.draw_boundary(img, d)
            elif draw_type == "platform_overlap_contact_anchors":
                PlatformOverlapRenderer.draw_contact_anchors(img, d)
            elif draw_type == "platform_overlap_region":
                PlatformOverlapRenderer.draw_region(img, d)

        return img