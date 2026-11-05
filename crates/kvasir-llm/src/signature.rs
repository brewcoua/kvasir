//! A signature: a model call's instructions and its typed input and output fields.

use serde::Serialize;
use serde::de::DeserializeOwned;
use serde_json::Value;

use crate::shape::{Kind, Shape, root_schema};

/// A call whose prompt dspy's `Signature` would have described. Field order is prompt order.
pub trait Signature {
    /// The Python class this replaces, as `module.Class`, for logs and the golden prompts.
    const NAME: &'static str;
    /// The prompt's instructions, from an embedded prompt file.
    const INSTRUCTIONS: &'static str;
    type Input: Serialize;
    type Output: DeserializeOwned;
    fn inputs() -> Vec<Field>;
    /// For what was a `dspy.ChainOfThought`, starts with [`Field::reasoning`].
    fn outputs() -> Vec<Field>;
}

/// One field of a signature.
#[derive(Debug, Clone)]
pub struct Field {
    pub name: &'static str,
    /// Shown to the model after the field's type. May be empty.
    pub description: &'static str,
    pub type_name: String,
    pub kind: Kind,
    /// pydantic's JSON Schema for the field's type.
    pub schema: Value,
    pub(crate) lax: fn(&Value) -> Option<Value>,
}

impl Field {
    pub fn of<T: Shape>(name: &'static str, description: &'static str) -> Self {
        Self {
            name,
            description,
            type_name: T::type_name(),
            kind: T::kind(),
            schema: root_schema::<T>(),
            lax: T::lax,
        }
    }

    /// The output `dspy.ChainOfThought` puts first.
    pub fn reasoning() -> Self {
        Self::of::<String>("reasoning", "")
    }
}
