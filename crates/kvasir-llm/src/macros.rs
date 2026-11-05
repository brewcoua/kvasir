//! Declaring signatures and the types of their fields in a few lines each.

/// Makes the enum `$name` a field type whose values are the strings `$values`, read back by `$lax`,
/// a `fn(&Value) -> Option<Value>`: [`crate::shape::exact`] for the strings alone.
#[macro_export]
macro_rules! choice {
    ($name:ty, $values:expr, $lax:expr) => {
        impl $crate::shape::Shape for $name {
            fn type_name() -> String {
                $crate::shape::literal_name($values)
            }
            fn kind() -> $crate::shape::Kind {
                $crate::shape::Kind::Literal($values)
            }
            fn schema(_: &mut serde_json::Map<String, serde_json::Value>) -> serde_json::Value {
                $crate::shape::literal_schema($values)
            }
            fn lax(value: &serde_json::Value) -> Option<serde_json::Value> {
                $lax(value)
            }
        }
    };
}

/// Makes the struct `$name` a field type described as pydantic describes a model: `$description`,
/// an `Option<&str>`, and `$props`, an array of [`crate::shape::Prop`] in field order.
#[macro_export]
macro_rules! model {
    ($name:ident, $description:expr, $props:expr) => {
        impl $crate::shape::Shape for $name {
            fn type_name() -> String {
                stringify!($name).into()
            }
            fn schema(defs: &mut serde_json::Map<String, serde_json::Value>) -> serde_json::Value {
                $crate::shape::model_schema(defs, stringify!($name), $description, &$props)
            }
            fn lax(value: &serde_json::Value) -> Option<serde_json::Value> {
                $crate::shape::model_lax(value, &$props)
            }
        }
    };
}

/// A module `$sig` holding `Input`, `Output` and `Sig`, the signature named `$name` whose
/// instructions are `$instructions`. Each field is `name: Type = "description"`, in prompt order.
#[macro_export]
macro_rules! signature {
    ($sig:ident, $name:expr, $instructions:expr,
     in { $($i:ident: $it:ty = $id:expr),* $(,)? },
     out { $($o:ident: $ot:ty = $od:expr),* $(,)? }) => {
        #[allow(non_camel_case_types, dead_code)]
        pub mod $sig {
            use super::*;
            #[derive(serde::Serialize, serde::Deserialize)]
            pub struct Input { $(pub $i: $it),* }
            #[derive(Debug, serde::Deserialize)]
            pub struct Output { $(pub $o: $ot),* }
            pub struct Sig;
            impl $crate::Signature for Sig {
                const NAME: &'static str = $name;
                const INSTRUCTIONS: &'static str = $instructions;
                type Input = Input;
                type Output = Output;
                fn inputs() -> Vec<$crate::Field> {
                    vec![$($crate::Field::of::<$it>(stringify!($i), $id)),*]
                }
                fn outputs() -> Vec<$crate::Field> {
                    vec![$($crate::Field::of::<$ot>(stringify!($o), $od)),*]
                }
            }
        }
    };
}
