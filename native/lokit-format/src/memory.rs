use crate::model::*;

trait RetainedBytes {
    fn heap_bytes(&self) -> usize;
}

impl RetainedBytes for String {
    fn heap_bytes(&self) -> usize { self.capacity() }
}

impl<T: RetainedBytes> RetainedBytes for Option<T> {
    fn heap_bytes(&self) -> usize { self.as_ref().map_or(0, RetainedBytes::heap_bytes) }
}

impl<T: RetainedBytes> RetainedBytes for Vec<T> {
    fn heap_bytes(&self) -> usize {
        self.iter().fold(self.capacity().saturating_mul(std::mem::size_of::<T>()), |size, item| size.saturating_add(item.heap_bytes()))
    }
}

impl<A: RetainedBytes, B: RetainedBytes> RetainedBytes for (A, B) {
    fn heap_bytes(&self) -> usize { self.0.heap_bytes().saturating_add(self.1.heap_bytes()) }
}

macro_rules! heap_fields {
    ($type:ty, $($field:ident),+ $(,)?) => {
        impl RetainedBytes for $type {
            fn heap_bytes(&self) -> usize { 0usize$(.saturating_add(self.$field.heap_bytes()))+ }
        }
    };
}

heap_fields!(Plural, variant, extensions);
heap_fields!(Meta, last_used, first_used, created, updated, extensions);
heap_fields!(Origin, system, project, creator_id, extensions);
heap_fields!(Comment, context, timestamp, origin, context_key, extensions);
heap_fields!(TextPart, value);
heap_fields!(CodePart, r#ref);
heap_fields!(TieData, id, attributes, attribute_data, pair_id, original_name, original_text);
heap_fields!(Tags, source_tag_map, target_tag_map, source_parts, target_parts);
heap_fields!(TargetTags, tag_map, parts);
heap_fields!(TargetData, text, tags, plural, meta, comments, extensions);
heap_fields!(AdjacentContext, unit_id, source, target, extensions);
heap_fields!(Data, source, target, targets, plural, tags, meta, comments, previous_context, next_context, extensions);

impl RetainedBytes for SegmentPart {
    fn heap_bytes(&self) -> usize {
        match self { Self::Text(text) => text.heap_bytes(), Self::Code(code) => code.heap_bytes() }
    }
}

impl Data {
    pub fn retained_bytes(&self) -> usize {
        std::mem::size_of::<Self>().saturating_add(self.heap_bytes())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn includes_nested_payloads_and_spare_capacity() {
        let mut data = Data::new("source");
        let before = data.retained_bytes();
        let mut comment = Comment::new(String::with_capacity(4096));
        comment.extensions.push(("key".into(), "value".repeat(100)));
        data.targets.push(("fr".into(), TargetData { comments: vec![comment], ..TargetData::default() }));
        assert!(data.retained_bytes() >= before + 4596);
    }
}
